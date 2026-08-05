#!/usr/bin/python3
"""Escape closes the chooser, and is deliberately dead once a transfer starts.

Aborting an upload should take a click on Cancel; a stray keypress on a window
that happens to have focus should not kill it halfway.
"""

import os
import sys
import tempfile

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Checks, load_action  # noqa: E402

check = Checks()
action = load_action()

probe = os.path.join(tempfile.mkdtemp(), "probe.txt")
open(probe, "w").write("x")


def press_escape(window):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = Gdk.KEY_Escape
    event.window = window.get_window()
    return window.emit("key-press-event", event)


# --- Escape in the chooser closes the window --------------------------------

print("=== Escape in the chooser ===")
window = action.SendWindow([probe])
window.show_all()
state = {"tries": 0, "handled": None, "timed_out": False}


def press_when_ready():
    state["tries"] += 1
    if not window.listbox.get_children() and state["tries"] < 100:
        return True
    state["handled"] = press_escape(window)
    return False


def watchdog():
    state["timed_out"] = True
    Gtk.main_quit()
    return False


GLib.timeout_add(100, press_when_ready)
GLib.timeout_add(6000, watchdog)
Gtk.main()

check("escape is handled", state["handled"], True)
check("main loop exited", state["timed_out"], False)

# --- Escape during a transfer does nothing ----------------------------------

print("\n=== Escape while transferring ===")


class FakeTransfer:
    def __init__(self):
        self.cancellable = Gio.Cancellable()


busy = action.SendWindow([probe])
busy.transfer = FakeTransfer()
busy.progress.show()

swallowed = press_escape(busy)
check("event swallowed, not passed on", swallowed, True)
check("transfer NOT cancelled by escape", busy.transfer.cancellable.is_cancelled(), False)
check("cancel button still enabled", busy.cancel_button.get_sensitive(), True)

busy._on_cancel(None)
check("cancel button still cancels", busy.transfer.cancellable.is_cancelled(), True)

sys.exit(check.report())
