#!/usr/bin/env bash
# Link the plugin into DaVinci Resolve's per-user Scripts/Edit folder.
#
#   ./install.sh            # symlink (edits in this repo take effect on next launch)
#   ./install.sh --copy     # copy instead of symlink
#   ./install.sh --uninstall
#
# Resolve scans the Scripts folders at startup: restart Resolve (or reopen the
# project) after installing so "Kaitoi Video" appears under
# Workspace > Scripts > Edit.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="Kaitoi Video.py"

case "$(uname -s)" in
  Darwin)
    TARGET_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit" ;;
  Linux)
    TARGET_DIR="$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Edit" ;;
  MINGW*|MSYS*|CYGWIN*)
    TARGET_DIR="$APPDATA/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Edit" ;;
  *)
    echo "Unsupported platform: $(uname -s)"; exit 1 ;;
esac

mkdir -p "$TARGET_DIR"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$TARGET_DIR/$LAUNCHER"
  rm -rf "$TARGET_DIR/kaitoi_resolve"
  echo "Removed from $TARGET_DIR"
  exit 0
fi

# Clear any earlier install so a copy never shadows a symlink or vice versa.
rm -f "$TARGET_DIR/$LAUNCHER"
rm -rf "$TARGET_DIR/kaitoi_resolve"

if [[ "${1:-}" == "--copy" ]]; then
  cp "$HERE/$LAUNCHER" "$TARGET_DIR/$LAUNCHER"
  cp -R "$HERE/kaitoi_resolve" "$TARGET_DIR/kaitoi_resolve"
  echo "Copied to $TARGET_DIR"
else
  ln -s "$HERE/$LAUNCHER" "$TARGET_DIR/$LAUNCHER"
  echo "Linked $TARGET_DIR/$LAUNCHER -> $HERE/$LAUNCHER"
fi

echo "Restart DaVinci Resolve, then open Workspace > Scripts > Edit > Kaitoi Video."
