"""Folder adapter: DeviceAdapter over a plain local folder.

Port of upstream LocalFolderAdapter.cs: the demo phone used when
APT_DEMO_FOLDER is set, so the UI can be tried without a phone. Also
the live-testable path for the device layer: everything here runs
against a real directory, no device needed.

Device id is "demo:<abspath>", stable key "DEMO:<abspath>", one
storage root (the folder itself, "Internal storage"). persistent_id
for a file is its full path, mirroring upstream.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from typing import BinaryIO, Callable

from .interface import DeviceAdapter
from .types import (
    DeviceDisconnectedError,
    DeviceEntry,
    DeviceError,
    PhoneInfo,
    PhoneListing,
    StorageRoot,
)

_log = logging.getLogger("android_photo_transfer")

DEVICE_ID_PREFIX = "demo:"
DEMO_NAME = "Demo Phone (folder)"
_LIST_BATCH_SIZE = 200


def list_phones(demo_folder: str) -> list[PhoneListing]:
    """The demo phone listing, or [] when the folder does not exist."""
    root = os.path.abspath(demo_folder)
    if not os.path.isdir(root):
        return []
    return [PhoneListing(DEVICE_ID_PREFIX + root, DEMO_NAME)]


def connect(device_id: str) -> "FolderAdapter":
    """Open the demo folder named by a "demo:<abspath>" device id."""
    if not device_id.startswith(DEVICE_ID_PREFIX):
        raise DeviceError(f"Not a demo-folder device id: {device_id!r}")
    root = device_id[len(DEVICE_ID_PREFIX):]
    if not os.path.isdir(root):
        raise DeviceDisconnectedError(
            f"The demo folder is gone: {root}"
        )
    return FolderAdapter(root)


class FolderAdapter(DeviceAdapter):
    """DeviceAdapter backed by a normal local folder."""

    def __init__(self, root: str):
        self._root = os.path.abspath(root)
        self._info = PhoneInfo(
            DEVICE_ID_PREFIX + self._root, DEMO_NAME, "DEMO:" + self._root
        )

    @property
    def info(self) -> PhoneInfo:
        return self._info

    @property
    def is_connected(self) -> bool:
        return os.path.isdir(self._root)

    def get_storage_roots(self) -> list[StorageRoot]:
        total = free = None
        try:
            usage = shutil.disk_usage(self._root)
            total, free = usage.total, usage.free
        except OSError:
            pass
        return [StorageRoot(self._root, "Internal storage", total, free)]

    def folder_has_entries(self, path: str) -> bool:
        try:
            with os.scandir(path) as it:
                return next(it, None) is not None
        except OSError as exc:
            raise DeviceError(
                f"Could not list folder {path!r}: {exc}"
            ) from exc

    def list_folder(
        self, path: str, on_batch: Callable[[list[DeviceEntry]], None]
    ) -> None:
        try:
            it = os.scandir(path)
        except OSError as exc:
            raise DeviceError(
                f"Could not list folder {path!r}: {exc}"
            ) from exc
        batch: list[DeviceEntry] = []
        with it:
            for entry in it:
                try:
                    is_folder = entry.is_dir()
                    stat = entry.stat()
                except OSError as exc:
                    # One unreadable entry must not kill the whole folder.
                    _log.warning("Skipped unreadable entry %r: %s", entry.path, exc)
                    continue
                batch.append(
                    DeviceEntry(
                        full_name=entry.path,
                        name=entry.name,
                        is_folder=is_folder,
                        is_hidden=entry.name.startswith("."),
                        size=0 if is_folder else stat.st_size,
                        created=None,  # Linux stat has no birth time
                        modified=datetime.fromtimestamp(stat.st_mtime),
                        persistent_id=None if is_folder else entry.path,
                    )
                )
                if len(batch) >= _LIST_BATCH_SIZE:
                    on_batch(batch)
                    batch = []
        if batch:
            on_batch(batch)

    def open_read(self, path: str, persistent_id: str | None) -> BinaryIO:
        target = persistent_id or path
        try:
            return open(target, "rb")
        except OSError as exc:
            raise DeviceError(f"Could not open {path!r}: {exc}") from exc

    def get_thumbnail(self, path: str, persistent_id: str | None) -> bytes | None:
        # No stored thumbnails; the UI falls back to decoding the image.
        return None

    def read_all_bytes(
        self, path: str, persistent_id: str | None, max_bytes: int
    ) -> bytes:
        target = persistent_id or path
        try:
            size = os.path.getsize(target)
        except OSError as exc:
            raise DeviceError(f"Could not read {path!r}: {exc}") from exc
        if size > max_bytes:
            raise ValueError(
                f"File is too large to load for preview ({size} bytes)."
            )
        try:
            with open(target, "rb") as f:
                return f.read()
        except OSError as exc:
            raise DeviceError(f"Could not read {path!r}: {exc}") from exc

    def delete_file(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError as exc:
            raise DeviceError(f"Could not delete {path!r}: {exc}") from exc

    def is_still_connected(self) -> bool:
        return os.path.isdir(self._root)

    def close(self) -> None:
        pass


__all__ = [
    "DEMO_NAME",
    "DEVICE_ID_PREFIX",
    "FolderAdapter",
    "connect",
    "list_phones",
]
