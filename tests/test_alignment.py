#!/usr/bin/python3
"""The tree indent must be a fixed number of pixels, not a function of the font.

Child rows have to clear the "<drive> /" prefix that only the exchange row
carries. Padding that with spaces would make the alignment drift with the font;
a Gtk.SizeGroup makes it exact. This measures where each row's folder icon
actually lands, at several fonts, and fails if the step is not constant.
"""

import os
import sys
import tempfile

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Checks, load_action  # noqa: E402

FONTS = ["Ubuntu 11", "Ubuntu 9", "Ubuntu 14", "DejaVu Serif 12"]

check = Checks()
action = load_action()

probe = os.path.join(tempfile.mkdtemp(), "probe.txt")
open(probe, "w").write("x")

measurements = []


def measure_with(font):
    Gtk.Settings.get_default().set_property("gtk-font-name", font)
    window = action.SendWindow([probe])
    window.show_all()
    state = {"tries": 0, "settle": 0}

    def measure():
        state["tries"] += 1
        rows = window.listbox.get_children()
        if not rows and state["tries"] < 100:
            return True
        state["settle"] += 1
        if state["settle"] < 8:      # let the resize land before measuring
            return True

        offsets = []
        for row in rows:
            images = [w for w in row.get_child().get_children()
                      if isinstance(w, Gtk.Image)]
            offsets.append(images[-1].get_allocation().x)   # the folder icon
        measurements.append((font, offsets))

        window.destroy()
        Gtk.main_quit()
        return False

    GLib.timeout_add(80, measure)
    Gtk.main()


for font in FONTS:
    measure_with(font)

print(f"{'font':18} {'exchange':>9} {'child 1':>9} {'child 2':>9}   step")
steps = set()
for font, offsets in measurements:
    steps.add(offsets[1] - offsets[0])
    aligned = len(set(offsets[1:])) == 1
    print(f"{font:18} {offsets[0]:>9} {offsets[1]:>9} {offsets[2]:>9}   "
          f"{offsets[1] - offsets[0]}px (children aligned: {'yes' if aligned else 'NO'})")

print()
check("children aligned with each other in every font",
      all(len(set(o[1:])) == 1 for _f, o in measurements), True)
check("indent step identical across fonts", len(steps), 1)
check("step equals INDENT_PX", steps, {action.INDENT_PX})

sys.exit(check.report())
