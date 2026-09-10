#!/usr/bin/env bash
# capture_screenshot.sh <output.png> [display]
#
# Grabs the screen using whatever tool the session actually provides.
#
# Tool choice is not a matter of taste here. On a Bazzite gamescope session the
# compositor is Wayland but applications reach it through XWayland, and `grim`
# and `scrot` are both absent while ImageMagick `import` is present -- so an
# XDG_SESSION_TYPE check alone picks the wrong tool. We probe for binaries
# instead, and only prefer grim when a Wayland socket is genuinely exported to
# us (an SSH session has none, even though the box is running Wayland).
set -uo pipefail

OUT="${1:?usage: capture_screenshot.sh <output.png> [display]}"
DISPLAY_ARG="${2:-${DISPLAY:-:0}}"

mkdir -p "$(dirname "$OUT")"
export DISPLAY="$DISPLAY_ARG"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

took_shot() { [ -s "$OUT" ]; }

if [ -n "${WAYLAND_DISPLAY:-}" ] && command -v grim >/dev/null 2>&1; then
    grim "$OUT" 2>/dev/null && took_shot && { echo "captured with grim: $OUT"; exit 0; }
fi

if command -v import >/dev/null 2>&1; then
    import -window root "$OUT" 2>/dev/null && took_shot && { echo "captured with import: $OUT"; exit 0; }
fi

if command -v scrot >/dev/null 2>&1; then
    scrot -o "$OUT" 2>/dev/null && took_shot && { echo "captured with scrot: $OUT"; exit 0; }
fi

if command -v spectacle >/dev/null 2>&1; then
    spectacle -b -n -o "$OUT" 2>/dev/null && took_shot && { echo "captured with spectacle: $OUT"; exit 0; }
fi

echo "capture_screenshot.sh: no working screenshot tool (tried grim, import, scrot, spectacle)" >&2
exit 1
