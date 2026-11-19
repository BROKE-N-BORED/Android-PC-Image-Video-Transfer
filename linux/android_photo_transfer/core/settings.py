"""User settings for the Android Photo Transfer Linux port.

Ports upstream Infrastructure/SettingsManager.cs (AppSettings) with the
Linux XDG layout from CONTRACT.md:

- settings JSON: ~/.config/android-photo-transfer/settings.json
- atomic save: write tmp file, then os.replace

A missing or corrupt file falls back to defaults (with a warning in the
log), exactly like upstream. The thumbnail cache size is clamped to
16..1024 MB on load and on assignment.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("android_photo_transfer")

CACHE_MIN_MB = 16
CACHE_MAX_MB = 1024


def config_dir() -> str:
    """~/.config/android-photo-transfer (honours XDG_CONFIG_HOME)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(config_home, "android-photo-transfer")


def data_dir() -> str:
    """~/.local/share/android-photo-transfer (honours XDG_DATA_HOME)."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(data_home, "android-photo-transfer")


def default_settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def default_history_path() -> str:
    return os.path.join(data_dir(), "history.db")


def default_destination() -> str:
    """~/Pictures/Phone Photos, per CONTRACT.md."""
    return os.path.join(os.path.expanduser("~"), "Pictures", "Phone Photos")


@dataclass
class Settings:
    dest_folder: str = field(default_factory=default_destination)
    skip_existing: bool = True
    open_folder_after_transfer: bool = False
    organize_by_folder: bool = False
    thumbnails_enabled: bool = True
    thumbnail_cache_mb: int = 64
    check_updates_on_startup: bool = True
    window_geometry: dict | None = None  # {"x", "y", "w", "h"}

    def __post_init__(self) -> None:
        self.dest_folder = os.path.expanduser(self.dest_folder or "") or default_destination()
        self.thumbnail_cache_mb = max(
            CACHE_MIN_MB, min(CACHE_MAX_MB, int(self.thumbnail_cache_mb))
        )
        if self.window_geometry is not None and not isinstance(
            self.window_geometry, dict
        ):
            self.window_geometry = None


class SettingsManager:
    """Loads settings once, saves atomically on demand."""

    def __init__(self, path: str | None = None):
        self._path = path or default_settings_path()
        self.current: Settings = self._load()

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> Settings:
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    known = {
                        key: value
                        for key, value in data.items()
                        if key in Settings.__dataclass_fields__
                    }
                    return Settings(**known)
        except Exception as exc:
            logger.warning(
                "Settings file could not be read; using defaults | %s: %s",
                type(exc).__name__,
                exc,
            )
        return Settings()

    def save(self) -> None:
        """Atomic save (tmp + os.replace). Failures only log a warning."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(asdict(self.current), handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.warning(
                "Settings could not be saved | %s: %s", type(exc).__name__, exc
            )
