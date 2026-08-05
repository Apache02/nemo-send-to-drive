#!/usr/bin/python3
"""Development tool: render the chooser to a PNG so the layout can be looked at.

    ./render_window.py single out.png
    ./render_window.py multi  out.png

Draws through Gtk.Widget.draw() rather than Gdk.pixbuf_get_from_window(), which
trips a cairo assertion on GTK 3.24 / Ubuntu 20.04.
"""

import os
import sys
import tempfile

import cairo
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load_action  # noqa: E402

if len(sys.argv) != 3 or sys.argv[1] not in ("single", "multi"):
    sys.exit(__doc__)

which, out = sys.argv[1], sys.argv[2]
action = load_action()

tmp = tempfile.mkdtemp()
first = os.path.join(tmp, "Quarterly report 2026.pdf")
open(first, "w").write("x" * 5000)
sources = [first]
if which == "multi":
    for name in ("notes.txt", "photo.jpg", "archive.tar.gz"):
        path = os.path.join(tmp, name)
        open(path, "w").write("y" * 100)
        sources.append(path)

window = action.SendWindow(sources)
window.show_all()
state = {"tries": 0, "settle": 0}


def grab():
    state["tries"] += 1
    rows = window.listbox.get_children()
    if not rows and state["tries"] < 100:
        return True
    state["settle"] += 1
    if state["settle"] < 8:          # let the resize-to-content land first
        return True

    alloc = window.get_allocation()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, alloc.width, alloc.height)
    window.draw(cairo.Context(surface))
    surface.write_to_png(out)
    print(f"saved {out}  {alloc.width}x{alloc.height}  rows={len(rows)}")

    for index, row in enumerate(rows):
        box = row.get_child()
        parts = []
        for widget in box.get_children():
            if isinstance(widget, Gtk.Image):
                parts.append(f"[{widget.get_gicon()[0].get_names()[0]}"
                             f"@{widget.get_pixel_size()}]")
            elif isinstance(widget, Gtk.Label):
                parts.append(repr(widget.get_text()))
            else:
                parts.append(f"<{type(widget).__name__} "
                             f"w={widget.get_allocation().width}>")
        print(f"  row {index}: indent={box.get_margin_start():3}  " + " ".join(parts))

    Gtk.main_quit()
    return False


GLib.timeout_add(100, grab)
Gtk.main()
