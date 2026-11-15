"""Small shared UI helpers for the Android Photo Transfer Linux port.

No Qt widgets with user-visible text live here except through the helpers
that open dialogs; every visible string in this package avoids bare ``&``
(mnemonic marker in Qt) and em-dashes.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QWidget

# Palette mirrored from upstream App.xaml brushes.
ACCENT = "#0067C0"
ACCENT_HOVER = "#1A76C7"
ACCENT_PRESSED = "#005BA8"
ACCENT_LIGHT = "#E3EEF9"
CHROME = "#F3F3F3"
SIDEBAR = "#F7F7F7"
DIVIDER = "#E1E1E1"
SUBTLE_TEXT = "#5F6368"
TEXT = "#1B1B1B"
SUCCESS = "#2E9E44"
WARNING = "#D88A00"
ERROR = "#C42B1C"
TILE_BG = "#EEF0F3"

APP_NAME = "Android Photo Transfer"


def xdg_data_home() -> Path:
    """XDG data dir, matching the contract paths."""
    override = os.environ.get("XDG_DATA_HOME")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share"


def logs_dir() -> Path:
    return xdg_data_home() / "android-photo-transfer" / "logs"


def format_bytes(size: int) -> str:
    """Human size, mirroring upstream FileUtilities.FormatBytes."""
    if size < 0:
        size = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    if value >= 100:
        return f"{value:.0f} {units[unit]}"
    if value >= 10:
        return f"{value:.1f} {units[unit]}"
    number = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{number} {units[unit]}"


def format_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%b %d, %Y  %I:%M %p").replace("  0", "  ")


def format_duration(total_seconds: float) -> str:
    total = int(total_seconds)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours >= 1:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def pick_folder(parent: QWidget | None, start: str, title: str) -> str | None:
    """Folder picker; returns None when the user cancels."""
    folder = QFileDialog.getExistingDirectory(parent, title, start or str(Path.home()))
    return folder or None


def open_folder(path: str) -> bool:
    """Open a folder in the system file manager (Linux: xdg-open)."""
    if not path:
        return False
    try:
        subprocess.Popen(
            ["xdg-open", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False
