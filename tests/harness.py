"""Shared plumbing for the tests.

The action is not an importable package - it is a standalone script Nemo
executes - so it gets loaded by path. Tests always load the copy in actions/,
never whatever happens to be installed in Nemo's folder.

Nothing here hardcodes a Google account: the exchange folder is discovered the
same way the action discovers it.
"""

import importlib.util
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "actions", "send-to-google-drive.py")

ATTRS = "standard::name,standard::display-name,standard::type,standard::size"


def load_action():
    spec = importlib.util.spec_from_file_location("send_to_google_drive", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exchange_folder(module):
    """The exchange folder on the mounted Drive, via the action's own lookup."""
    return module.load_targets()[0].folder


def kids(folder):
    return list(folder.enumerate_children(ATTRS, Gio.FileQueryInfoFlags.NONE, None))


def by_name(folder, name):
    for info in kids(folder):
        if info.get_display_name() == name:
            return folder.get_child(info.get_name())
    return None


def read(gfile):
    ok, data, _etag = gfile.load_contents(None)
    return data.decode().strip() if ok else "<unreadable>"


def purge(folder):
    """Delete a folder's contents depth-first; Drive has no recursive delete."""
    for info in kids(folder):
        child = folder.get_child(info.get_name())
        if info.get_file_type() == Gio.FileType.DIRECTORY:
            purge(child)
        child.delete(None)


class Checks:
    """Tiny assertion collector - keeps output readable without pytest."""

    def __init__(self):
        self.failures = []

    def __call__(self, label, got, want):
        ok = got == want
        if not ok:
            self.failures.append(label)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:44} got={got!r}")
        return ok

    def report(self):
        if self.failures:
            print(f"\nRESULT: {len(self.failures)} FAILED: {self.failures}")
            return 1
        print("\nRESULT: ALL PASS")
        return 0
