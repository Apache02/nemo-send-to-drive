#!/bin/bash
# Link the action into Nemo's actions folder.
#
# Symlinks rather than copies, so editing the repo takes effect on the next
# right-click with no reinstall step. Nemo resolves <script> in an Exec line
# against the actions folder, and following a symlink there works fine.
#
#   ./install.sh          link into ~/.local/share/nemo/actions
#   ./install.sh --undo   remove the links again

set -euo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nemo/actions"
FILES=(send-to-google-drive.py send-to-google-drive.nemo_action)

if [[ "${1:-}" == "--undo" ]]; then
    for f in "${FILES[@]}"; do
        link="$TARGET_DIR/$f"
        if [ -L "$link" ]; then
            rm "$link"
            echo "removed  $link"
        else
            echo "skipped  $link (not a symlink)"
        fi
    done
    echo
    echo "Run 'nemo -q' to make Nemo forget the action."
    exit 0
fi

mkdir -p "$TARGET_DIR"
chmod +x "$REPO/actions/send-to-google-drive.py"

for f in "${FILES[@]}"; do
    source_file="$REPO/actions/$f"
    link="$TARGET_DIR/$f"

    # An earlier hand-installed copy would silently win over the repo, so move
    # it out of the way instead of overwriting it.
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        mv "$link" "$link.bak"
        echo "backed up  $link -> $link.bak"
    fi

    ln -sfn "$source_file" "$link"
    echo "linked   $link -> $source_file"
done

echo
echo "Installed. Run 'nemo -q' if Nemo is already running, then right-click a"
echo "file and look for “Send to Google Drive/exchange…”."
