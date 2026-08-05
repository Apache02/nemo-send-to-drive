#!/usr/bin/python3
"""Exercise the copy engine through --device (no GUI).

Covers the awkward parts: non-ASCII and shell-hostile file names, nested
folders, overwriting, and the two failure modes this design exists to avoid -
truncated files caused by the FUSE write lag, and duplicate entries caused by
Google Drive allowing two children with the same name.

Creates a throwaway __pytest__ folder inside exchange and removes it again.
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Checks, SCRIPT, by_name, exchange_folder, kids, load_action, purge, read

DEVICE = "__pytest__"

check = Checks()
action = load_action()
exchange = exchange_folder(action)

if by_name(exchange, DEVICE) is None:
    exchange.get_child(DEVICE).make_directory(None)
device = by_name(exchange, DEVICE)
print(f"test device folder: {DEVICE} ({device.get_basename()})")

# --- fixtures ---------------------------------------------------------------

# Deliberately hostile: 1-, 2-, 3- and 4-byte UTF-8 sequences, a percent sign
# that percent-decoding could eat, an apostrophe and an ampersand that would
# break naive shell quoting.
AWKWARD_NAME = "unicode ü 中 🚀 + 100% & 'quoted' file.txt"

fixtures = tempfile.mkdtemp()
os.makedirs(f"{fixtures}/dir with space/nested/deeper")
open(f"{fixtures}/plain.txt", "w").write("one\n")
open(f"{fixtures}/{AWKWARD_NAME}", "w").write("two\n")
open(f"{fixtures}/dir with space/top.txt", "w").write("L1\n")
open(f"{fixtures}/dir with space/nested/mid.txt", "w").write("L2\n")
open(f"{fixtures}/dir with space/nested/deeper/deep.txt", "w").write("L3\n")
for i in range(1, 7):
    open(f"{fixtures}/dir with space/f{i}.bin", "w").write("x" * 1000)

import gi  # noqa: E402
gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

uris = [
    Gio.File.new_for_path(f"{fixtures}/{name}").get_uri()
    for name in ("plain.txt", AWKWARD_NAME, "dir with space")
]


def send(label, args):
    result = subprocess.run([SCRIPT, *args], capture_output=True, text=True)
    print(f"\n--- {label}: exit={result.returncode} ---")
    if result.stderr.strip():
        print("   stderr:", result.stderr.strip())
    return result


first = send("first send", ["--device", DEVICE, *uris])

# --- the write lag this design avoids ---------------------------------------

print("\n=== sizes read back IMMEDIATELY (write-lag check) ===")
folder = by_name(device, "dir with space")
truncated = [
    info.get_display_name()
    for info in kids(folder)
    if info.get_file_type() != Gio.FileType.DIRECTORY
    and info.get_size() != (1000 if info.get_display_name().endswith(".bin") else 3)
]
for info in sorted(kids(folder), key=lambda i: i.get_display_name()):
    if info.get_file_type() != Gio.FileType.DIRECTORY:
        print(f"  {info.get_display_name():14} size={info.get_size()}")
check("no truncated files immediately after copy", truncated, [])

# --- names and depth ---------------------------------------------------------

print("\n=== exit code, tricky names, depth ===")
check("exit code", first.returncode, 0)
check("awkward unicode / shell-hostile name",
      read(by_name(device, AWKWARD_NAME)), "two")
check("plain file", read(by_name(device, "plain.txt")), "one")
check("depth 1", read(by_name(folder, "top.txt")), "L1")
nested = by_name(folder, "nested")
check("depth 2", read(by_name(nested, "mid.txt")), "L2")
check("depth 3", read(by_name(by_name(nested, "deeper"), "deep.txt")), "L3")

# --- resending must overwrite, not duplicate ---------------------------------

print("\n=== second send: overwrite, no duplicates ===")
open(f"{fixtures}/plain.txt", "w").write("UPDATED\n")
send("second send", ["--device", DEVICE, *uris])

top_names = [i.get_display_name() for i in kids(device)]
inner_names = [i.get_display_name() for i in kids(by_name(device, "dir with space"))]
check("no duplicate entries at top level", len(top_names) - len(set(top_names)), 0)
check("no duplicate entries inside folder", len(inner_names) - len(set(inner_names)), 0)
check("existing file overwritten", read(by_name(device, "plain.txt")), "UPDATED")

# --- argument handling --------------------------------------------------------

print("\n=== bad --device name is refused ===")
bad = subprocess.run([SCRIPT, "--device", "no-such-device", uris[0]],
                     capture_output=True, text=True)
check("unknown device exits non-zero", bad.returncode, 1)
check("unknown device explains itself", "No device folder" in bad.stderr, True)

print("\n=== no arguments ===")
empty = subprocess.run([SCRIPT, "--device", DEVICE], capture_output=True, text=True)
check("empty selection exits non-zero", empty.returncode, 1)

# --- exchange itself is a target ----------------------------------------------

print("\n=== exchange root is a valid target ===")
marker = os.path.join(fixtures, "root-target-probe.txt")
open(marker, "w").write("ROOT\n")
root_send = subprocess.run(
    [SCRIPT, "--device", "exchange", Gio.File.new_for_path(marker).get_uri()],
    capture_output=True, text=True,
)
check("send to exchange root exits 0", root_send.returncode, 0)
landed = by_name(exchange, "root-target-probe.txt")
check("file landed in exchange root", landed is not None, True)
if landed is not None:
    check("content intact", read(landed), "ROOT")
    landed.delete(None)
check("probe removed from exchange root",
      by_name(exchange, "root-target-probe.txt") is None, True)

# --- cleanup -------------------------------------------------------------------

purge(device)
device.delete(None)
shutil.rmtree(fixtures)
print("\ncleanup done. exchange now:",
      [i.get_display_name() for i in kids(exchange)])

sys.exit(check.report())
