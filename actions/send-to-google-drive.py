#!/usr/bin/python3
"""Nemo action helper: copy the selection into Google Drive under exchange/<device>.

Invoked from send-to-google-drive.nemo_action as:  <send-to-google-drive.py %U>
Arguments are percent-encoded file:// URIs of the selected items.

    --device NAME   skip the chooser and send straight to that device folder

Everything goes through the native gvfs backend rather than the FUSE mount at
/run/user/UID/gvfs. That matters: writes over FUSE are not committed by the
time the write call returns, so the last file of a batch reads back as 0 bytes
for several seconds. Gio.File.copy() commits before returning.

Google Drive allows several children with the same name in one folder, so every
destination folder is resolved by display name and reused instead of created
blindly - otherwise repeated sends would pile up duplicates.

Files already on Google Drive are overwritten without asking: exchange is a
drop box for moving things between machines, not storage.

Each run appends to ~/.cache/send-to-google-drive.log, the first place to look
when the menu entry misbehaves.
"""

import os
import sys
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("Notify", "0.7")
from gi.repository import GLib, Gdk, Gio, Gtk, Notify, Pango  # noqa: E402

APP_NAME = "Send to Google Drive"
EXCHANGE = "exchange"
# Each of these is a list of candidates because icon themes differ in what they
# ship. The gvfs mount only offers a generic drive-removable-media, so the
# Google logo from GNOME Online Accounts is preferred for the account row.
DRIVE_ICONS = ("goa-account-google", "drive-removable-media", "folder-remote")
EXCHANGE_ICONS = ("folder-publicshare", "folder-remote", "folder")
MULTIPLE_ICONS = ("document-multiple", "edit-copy", "document-send", "folder")

ICON_PX = 20
INDENT_PX = 16  # one tree step, applied on top of the size-grouped prefix
LOG = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "send-to-google-drive.log",
)


def log(message):
    try:
        with open(LOG, "a") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass


# --- Google Drive access -----------------------------------------------------

ATTRS = "standard::name,standard::display-name,standard::type,standard::size,standard::icon"


def children(folder):
    try:
        return list(folder.enumerate_children(ATTRS, Gio.FileQueryInfoFlags.NONE, None))
    except GLib.Error:
        return []


def child_by_name(folder, name, want_dir=None):
    """Resolve a child by display name: paths on Drive are addressed by file id."""
    for info in children(folder):
        if info.get_display_name() != name:
            continue
        is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
        if want_dir is not None and is_dir != want_dir:
            continue
        return folder.get_child(info.get_name())
    return None


def find_drive_root():
    for mount in Gio.VolumeMonitor.get().get_mounts():
        root = mount.get_root()
        if root.get_uri_scheme() == "google-drive":
            return root
    return None


class NotReady(Exception):
    """Drive is missing something the action cannot work without."""


class Target:
    """A folder the selection can be sent to, and how to draw it in the tree.

    `lead_icon` is only set for the exchange folder, which is rendered as the
    account followed by a path separator: <drive> / <folder> exchange.
    """

    def __init__(self, folder, name, icon, depth=0, lead_icon=None):
        self.folder = folder
        self.name = name
        self.icon = icon
        self.depth = depth
        self.lead_icon = lead_icon


def load_targets():
    """The exchange folder itself, followed by its device subfolders. Blocking."""
    drive = find_drive_root()
    if drive is None:
        raise NotReady(
            "Google Drive is not mounted.\n\n"
            "Open it once from the Nemo sidebar, then try again."
        )

    exchange = child_by_name(drive, EXCHANGE, want_dir=True)
    if exchange is None:
        raise NotReady(f"There is no “{EXCHANGE}” folder at the root of Google Drive.")

    devices = [
        Target(
            exchange.get_child(info.get_name()),
            info.get_display_name(),
            info.get_icon() or Gio.ThemedIcon.new("folder"),
            depth=1,
        )
        for info in children(exchange)
        if info.get_file_type() == Gio.FileType.DIRECTORY
    ]
    devices.sort(key=lambda t: t.name)

    root = Target(
        exchange,
        EXCHANGE,
        first_available_icon(EXCHANGE_ICONS),
        depth=0,
        lead_icon=first_available_icon(DRIVE_ICONS),
    )
    return [root] + devices


def local_icon(path):
    """Themed icon for a local file, so the header shows what Nemo shows."""
    try:
        info = Gio.File.new_for_path(path).query_info(
            "standard::icon", Gio.FileQueryInfoFlags.NONE, None
        )
        return info.get_icon()
    except GLib.Error:
        return None


def first_available_icon(names):
    """First of `names` the current icon theme actually has, else None.

    Asking for a missing icon leaves an empty gap in the row, and themes vary:
    OneUI has no document-multiple, for instance.
    """
    theme = Gtk.IconTheme.get_default()
    for name in names:
        if theme.has_icon(name):
            return Gio.ThemedIcon.new(name)
    return None


def icon_image(gicon):
    """A Gtk.Image at a fixed ICON_PX, so rows and header line up."""
    image = Gtk.Image.new_from_gicon(gicon, Gtk.IconSize.LARGE_TOOLBAR)
    image.set_pixel_size(ICON_PX)
    return image


def ensure_dir(parent, name):
    existing = child_by_name(parent, name, want_dir=True)
    if existing is not None:
        return existing
    parent.get_child(name).make_directory(None)
    created = child_by_name(parent, name, want_dir=True)
    if created is None:
        raise NotReady(f"Could not create the folder “{name}” on Google Drive.")
    return created


# --- copy engine -------------------------------------------------------------


def survey(paths):
    """Total bytes and file count behind a selection."""
    total_bytes = total_files = 0
    for path in paths:
        if os.path.isfile(path):
            total_bytes += os.path.getsize(path)
            total_files += 1
            continue
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = os.path.join(root, name)
                if os.path.isfile(fp) and not os.path.islink(fp):
                    total_bytes += os.path.getsize(fp)
                    total_files += 1
    return total_bytes, total_files


class Transfer:
    """Copies a selection into one destination folder, reporting progress.

    on_progress(fraction, label) is called from the worker thread; the UI layer
    is responsible for bouncing it onto the main loop.
    """

    def __init__(self, sources, dest_folder, on_progress):
        self.sources = sources
        self.dest = dest_folder
        self.on_progress = on_progress
        self.cancellable = Gio.Cancellable()
        self.total_bytes, self.total_files = survey(sources)
        self.done_bytes = 0
        self.done_files = 0
        self.failures = []

    # -- progress plumbing
    def _report(self, current_file_bytes=0, label=""):
        fraction = 0.0
        if self.total_bytes:
            fraction = min((self.done_bytes + current_file_bytes) / self.total_bytes, 1.0)
        self.on_progress(fraction, label)

    def _file_progress(self, current, _total, label):
        self._report(current, label)

    # -- copying
    def _copy_file(self, src_path, dest_folder, name):
        self._report(0, f"{self.done_files + 1} of {self.total_files} · {name}")
        src = Gio.File.new_for_path(src_path)
        src.copy(
            dest_folder.get_child(name),
            Gio.FileCopyFlags.OVERWRITE,
            self.cancellable,
            self._file_progress,
            f"{self.done_files + 1} of {self.total_files} · {name}",
        )
        self.done_bytes += os.path.getsize(src_path)
        self.done_files += 1

    def _copy_tree(self, src_path, dest_parent, name):
        dest_folder = ensure_dir(dest_parent, name)
        for entry in sorted(os.listdir(src_path)):
            if self.cancellable.is_cancelled():
                return
            child = os.path.join(src_path, entry)
            if os.path.islink(child):
                continue  # never follow links: they can point outside or loop
            if os.path.isdir(child):
                self._copy_tree(child, dest_folder, entry)
            elif os.path.isfile(child):
                self._copy_file(child, dest_folder, entry)

    def run(self):
        for path in self.sources:
            if self.cancellable.is_cancelled():
                break
            name = os.path.basename(path.rstrip("/"))
            try:
                if os.path.isdir(path):
                    self._copy_tree(path, self.dest, name)
                else:
                    self._copy_file(path, self.dest, name)
            except GLib.Error as exc:
                if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    break
                self.failures.append((name, exc.message))
            except (OSError, NotReady) as exc:
                self.failures.append((name, str(exc)))


# --- user interface ----------------------------------------------------------


def error_dialog(message, parent=None):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE,
        text=APP_NAME,
    )
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()


class SendWindow(Gtk.Window):
    """One window for the whole job: pick a target, then watch it upload.

    The target list is fetched on a worker thread so the window appears
    immediately even when Drive is slow to answer. Picking a row starts the
    transfer straight away, so there is no confirm button.
    """

    def __init__(self, sources):
        super().__init__(title=APP_NAME)
        self.sources = sources
        self.targets = []
        self.transfer = None
        self.destination_name = ""
        # Keeps every row's leading "<drive> /" slot the same width, so the
        # folder icons line up into a column instead of drifting with the font.
        self.prefix_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.HORIZONTAL)

        self.set_default_size(420, -1)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("folder-remote")
        self.connect("destroy", lambda *_: self._quit())
        self.connect("key-press-event", self._on_key_press)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(12)
        self.add(outer)

        outer.pack_start(self._build_header(), False, False, 0)

        # target list
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_shadow_type(Gtk.ShadowType.IN)
        # Height follows the number of targets instead of a fixed guess, and
        # only starts scrolling once the list gets genuinely long.
        self.scroller.set_propagate_natural_height(True)
        self.scroller.set_max_content_height(420)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        # GtkListBox activates on a single click, which is the whole interaction.
        self.listbox.connect("row-activated", self._on_row_activated)
        self.scroller.add(self.listbox)
        outer.pack_start(self.scroller, True, True, 0)

        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.loading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.loading.set_halign(Gtk.Align.CENTER)
        self.loading.pack_start(self.spinner, False, False, 0)
        self.loading.pack_start(Gtk.Label(label="Reading Google Drive…"), False, False, 0)
        outer.pack_start(self.loading, False, False, 0)

        # progress
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_no_show_all(True)
        outer.pack_start(self.progress, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._on_cancel)
        buttons.pack_start(self.cancel_button, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

        threading.Thread(target=self._load, daemon=True).start()

    def _build_header(self):
        """Reads "Send <icon> <name> to:" - the icon sits with what it labels.

        For a multi-selection there is no single file to show, so it falls back
        to a generic icon and a count.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        if len(self.sources) == 1:
            icon = local_icon(self.sources[0])
            what = os.path.basename(self.sources[0].rstrip("/"))
        else:
            icon = first_available_icon(MULTIPLE_ICONS)
            what = f"{len(self.sources)} items"

        box.pack_start(Gtk.Label(label="Send"), False, False, 0)
        if icon is not None:
            box.pack_start(icon_image(icon), False, False, 0)

        self.heading = Gtk.Label(xalign=0)
        self.heading.set_markup(f"<b>{GLib.markup_escape_text(what)}</b> to:")
        self.heading.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.pack_start(self.heading, True, True, 0)
        return box

    def _build_row(self, target):
        """One tree row: nesting is shown by indentation, not by glyphs.

        Child rows have to clear the "<drive> /" prefix that only the exchange
        row carries. Padding that gap with spaces would tie the alignment to
        the font, so an empty box is put in a size group with the real prefix
        instead: GTK then gives both the same width, whatever the theme does.
        INDENT_PX is the tree step on top of that, so a child sits exactly one
        step to the right of its parent's folder icon.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(7)
        box.set_margin_bottom(7)
        box.set_margin_start(10 + target.depth * INDENT_PX)
        box.set_margin_end(10)

        prefix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if target.lead_icon is not None:
            prefix.pack_start(icon_image(target.lead_icon), False, False, 0)
            slash = Gtk.Label(label="/")
            slash.get_style_context().add_class("dim-label")
            prefix.pack_start(slash, False, False, 0)
        self.prefix_group.add_widget(prefix)
        box.pack_start(prefix, False, False, 0)

        if target.icon is not None:
            box.pack_start(icon_image(target.icon), False, False, 0)
        box.pack_start(Gtk.Label(label=target.name, xalign=0), False, False, 0)

        row = Gtk.ListBoxRow()
        row.add(box)
        return row

    # -- loading the target list
    def _load(self):
        try:
            GLib.idle_add(self._loaded, load_targets())
        except NotReady as exc:
            GLib.idle_add(self._load_failed, str(exc))

    def _loaded(self, targets):
        self.targets = targets
        self.loading.hide()

        for target in targets:
            self.listbox.add(self._build_row(target))

        self.listbox.show_all()
        # Preselect the first device rather than the exchange root: sending to a
        # specific machine is the common case. Only matters for keyboard use.
        first_device = 1 if len(targets) > 1 else 0
        self.listbox.select_row(self.listbox.get_row_at_index(first_device))
        self.listbox.grab_focus()

        self._fit()
        return False

    def _fit(self):
        """Resize to the natural height of the current contents.

        The window is first sized while the list is still empty and GTK will
        not grow it afterwards. resize(w, 1) is no good either: that snaps to
        the *minimum* height, and a scrolled list can shrink to almost nothing.
        """
        _minimum, natural = self.get_preferred_size()
        self.resize(420, natural.height)

    def _load_failed(self, message):
        log(f"ERROR: {message}")
        self.hide()
        error_dialog(message)
        self._quit()
        return False

    # -- running the transfer
    def _on_row_activated(self, _listbox, row):
        if self.transfer is not None or row is None:
            return  # already sending; ignore a second click
        target = self.targets[row.get_index()]
        self.destination_name = target.name
        log(f"destination: {target.name} ({target.folder.get_basename()})")

        self.heading.set_markup(
            f"Sending to <b>{GLib.markup_escape_text(target.name)}</b>"
        )
        self.scroller.hide()
        self.progress.show()
        self.progress.set_text("Preparing…")
        self._fit()

        self.transfer = Transfer(self.sources, target.folder, self._on_progress)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        self.transfer.run()
        GLib.idle_add(self._finished)

    def _on_progress(self, fraction, label):
        GLib.idle_add(self._apply_progress, fraction, label)

    def _apply_progress(self, fraction, label):
        self.progress.set_fraction(fraction)
        if label:
            self.progress.set_text(label)
        return False

    def _on_key_press(self, _widget, event):
        """Escape closes the chooser, but is swallowed once a transfer starts.

        Aborting an upload should take a deliberate click on Cancel, not a
        stray keypress on a window that happens to have focus.
        """
        if event.keyval == Gdk.KEY_Escape:
            if self.transfer is None:
                self._on_cancel(None)
            return True
        return False

    def _on_cancel(self, _button):
        if self.transfer is not None:
            self.transfer.cancellable.cancel()
            self.progress.set_text("Cancelling…")
            self.cancel_button.set_sensitive(False)
        else:
            self._quit()

    def _finished(self):
        transfer = self.transfer
        self.hide()

        if transfer.cancellable.is_cancelled():
            log(f"cancelled after {transfer.done_files} of {transfer.total_files} file(s)")
        elif transfer.failures:
            detail = "\n".join(f"{n}: {m}" for n, m in transfer.failures[:10])
            log(f"FAILED {len(transfer.failures)} item(s): {detail}")
            error_dialog(
                f"Could not send {len(transfer.failures)} of "
                f"{len(self.sources)} item(s):\n\n{detail}"
            )
        else:
            log(f"sent {transfer.total_files} file(s) to {self.destination_name}")
            notify(
                f"Sent {transfer.total_files} file"
                f"{'s' if transfer.total_files != 1 else ''} "
                f"to {self.destination_name}"
            )

        self._quit()
        return False

    def _quit(self):
        if Gtk.main_level():
            Gtk.main_quit()


def notify(body):
    try:
        Notify.init(APP_NAME)
        note = Notify.Notification.new(APP_NAME, body, "folder-remote")
        note.show()
    except GLib.Error:
        pass


# --- entry point --------------------------------------------------------------


def parse_args(argv):
    device = None
    uris = []
    it = iter(argv)
    for arg in it:
        if arg == "--device":
            device = next(it, None)
        else:
            uris.append(arg)
    return device, uris


def headless_send(device_name, sources):
    """--device path: no chooser, no window. Used for scripting and tests."""
    targets = load_targets()
    match = next((t for t in targets if t.name == device_name), None)
    if match is None:
        raise NotReady(
            f"No device folder called “{device_name}”. "
            f"Available: {', '.join(t.name for t in targets)}"
        )
    log(f"destination: {device_name} ({match.folder.get_basename()}) [--device]")
    transfer = Transfer(sources, match.folder, lambda *_: None)
    transfer.run()
    if transfer.failures:
        for name, message in transfer.failures:
            log(f"FAILED {name}: {message}")
            print(f"{name}: {message}", file=sys.stderr)
        return 1
    log(f"sent {transfer.total_files} file(s) to {device_name}")
    return 0


def main(argv):
    device, uris = parse_args(argv)
    log(f"invoked with {len(uris)} argument(s)")

    if not uris:
        error_dialog("Nothing selected.")
        return 1

    sources = []
    for uri in uris:
        path = Gio.File.new_for_uri(uri.strip('"')).get_path()
        if path is None:
            error_dialog(f"Not a local file:\n\n{uri}")
            return 1
        sources.append(path)

    if device is not None:
        try:
            return headless_send(device, sources)
        except NotReady as exc:
            log(f"ERROR: {exc}")
            print(exc, file=sys.stderr)
            return 1

    SendWindow(sources).show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
