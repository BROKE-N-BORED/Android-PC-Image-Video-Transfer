"""Transfer history for the Android Photo Transfer Linux port.

Ports upstream Data/HistoryDatabase.cs with the simpler table from
CONTRACT.md: transfers(device_key, source_path, source_size) with the
three columns as the primary key. SQLite, WAL mode, default path
~/.local/share/android-photo-transfer/history.db.

All failures raise; callers catch and log (history must never be fatal).
"""
from __future__ import annotations

import os
import sqlite3
import threading

from .settings import default_history_path


class HistoryDatabase:
    """SQLite-backed record of which files have been copied, per device."""

    def __init__(self, path: str | None = None):
        self._path = path or default_history_path()
        self._gate = threading.Lock()
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        try:
            # Write-ahead logging: recording each copied file costs well
            # under a millisecond instead of a disk flush.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS transfers ("
                "device_key TEXT NOT NULL, "
                "source_path TEXT NOT NULL, "
                "source_size INTEGER NOT NULL, "
                "PRIMARY KEY (device_key, source_path, source_size))"
            )
            self._db.commit()
        except Exception:
            self._db.close()
            raise

    @property
    def path(self) -> str:
        return self._path

    def record(self, device_key: str, source_path: str, size: int) -> None:
        with self._gate:
            self._db.execute(
                "INSERT OR REPLACE INTO transfers "
                "(device_key, source_path, source_size) VALUES (?, ?, ?)",
                (device_key, source_path, size),
            )
            self._db.commit()

    def get_transferred_keys(self, device_key: str) -> set[str]:
        """Keys "path|size" for everything recorded for this device."""
        with self._gate:
            rows = self._db.execute(
                "SELECT source_path, source_size FROM transfers "
                "WHERE device_key = ?",
                (device_key,),
            ).fetchall()
        return {f"{path}|{size}" for path, size in rows}

    def close(self) -> None:
        with self._gate:
            self._db.close()

    def __enter__(self) -> "HistoryDatabase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
