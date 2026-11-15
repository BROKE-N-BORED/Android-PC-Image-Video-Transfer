#!/usr/bin/env bash
# Launcher for Android Photo Transfer (Linux port).
# Run from anywhere: changes to this script's directory and starts the app.
set -e
cd "$(dirname "$0")"
exec python3 -m android_photo_transfer "$@"
