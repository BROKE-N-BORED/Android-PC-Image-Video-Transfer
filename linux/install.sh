#!/bin/bash
# Android Photo Transfer installer (Linux Mint).
#
# Double-click "Install Android Photo Transfer" instead if you can; this
# script is the same installer for the terminal. Run it with BASH:
#     bash install.sh
# (never `sh install.sh`: this script needs bash.)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "FAILED: python3 was not found. Install it first with:"
    echo "  sudo apt install python3"
    exit 1
fi

# All install steps live in install_lib.py (shared with the GUI installer).
exec python3 "$DIR/install_lib.py" "$@"
