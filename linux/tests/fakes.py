"""Shared fakes and helpers for the android_photo_transfer test suite.

Mirrors upstream AndroidPhotoTransfer.Tests/UnitTest1.cs (FakePhone,
FakeStream, TempFolder, TestData), ported to the Linux contract
(port/CONTRACT.md): the Python DeviceAdapter interface (blocking methods,
"/"-separated device paths) and the Python MediaItem dataclass.

FakeAdapter is a real DeviceAdapter subclass over an in-memory file tree,
so scanner / transfer / device tests run with no phone and no libmtp.
"""
from __future__ import annotations

import io
import os
import random
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from android_photo_transfer.device.interface import DeviceAdapter
from android_photo_transfer.device.types import (
    DeviceEntry,
    DeviceDisconnectedError,
    PhoneInfo,
    StorageRoot,
)

# ---------------------------------------------------------------------------
# MediaItem: prefer the real one from core.scanner; fall back to a
# contract-identical dataclass so fakes.py (and the tests that only need the
# fake) keep working while the core module is still being built in parallel.
# ---------------------------------------------------------------------------

try:
    from android_photo_transfer.core.scanner import MediaItem as _RealMediaItem
except Exception:  # pragma: no cover - core module not built yet
    _RealMediaItem = None


if _RealMediaItem is not None:
    MediaItem = _RealMediaItem
else:

    @dataclass
    class MediaItem:  # type: ignore[no-redef]
        """Fallback matching CONTRACT core/scanner.py MediaItem exactly."""

        source_path: str
        name: str
        size: int
        modified: datetime | None
        created: datetime | None
        folder: str
        folder_category: str
        kind: str
        file_category: str
        persistent_id: str | None
        device_key: str
        already_transferred: bool = False


def _classify(name: str) -> tuple[str, str]:
    """Minimal classifier used only by the fallback path and make_media_item.

    Mirrors MediaClassifier.cs extension sets (see test_classifier.py for
    the authoritative assertions against the real classifier).
    """
    try:
        from android_photo_transfer.core import classifier as _c

        kind = "photo" if _c.is_photo(name) else "video" if _c.is_video(name) else "file"
        return kind, _c.file_category(name)
    except Exception:
        ext = os.path.splitext(name)[1].lower()
        photos = {".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".webp",
                  ".gif", ".bmp", ".tif", ".tiff", ".avif", ".dng", ".raw",
                  ".arw", ".cr2", ".cr3", ".nef", ".orf", ".rw2"}
        videos = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mkv", ".avi",
                  ".webm", ".wmv", ".mts", ".m2ts", ".ts"}
        if ext in photos:
            return "photo", "Photos"
        if ext in videos:
            return "video", "Videos"
        return "file", "Other"


def _folder_category(folder: str) -> str:
    try:
        from android_photo_transfer.core import classifier as _c

        return _c.folder_category(folder)
    except Exception:
        f = folder.replace("\\", "/").lower()
        if "screenshot" in f:
            return "Screenshots"
        if f == "dcim" or f.startswith("dcim/"):
            return "Camera"
        if f in ("download", "downloads") or f.startswith(("download/", "downloads/")):
            return "Downloads"
        if f == "pictures" or f.startswith("pictures/"):
            return "Pictures"
        return "Other"


def make_media_item(name: str, size: int, folder: str = "DCIM/Camera",
                    day: int = 1, kind: str | None = None,
                    device_key: str = "fake-device-id",
                    already_transferred: bool = False,
                    persistent_id: str | None = None) -> MediaItem:
    """Build a MediaItem directly (for query / destination / delete tests)."""
    folder = folder.strip("/")
    source_path = f"{FakeAdapter.ROOT}/{folder}/{name}" if folder else f"{FakeAdapter.ROOT}/{name}"
    real_kind, file_category = _classify(name)
    kind = kind or real_kind
    date = datetime(2026, 10, day, 12, 0, 0)
    return MediaItem(
        source_path=source_path,
        name=name,
        size=size,
        modified=date,
        created=date,
        folder=folder,
        folder_category=_folder_category(folder),
        kind=kind,
        file_category=file_category,
        persistent_id=persistent_id or f"PUID-{name}-{size}",
        device_key=device_key,
        already_transferred=already_transferred,
    )


def try_import(dotted: str):
    """Import a module by dotted name, returning None instead of raising.

    Lets every test module skip cleanly while the parallel builders are
    still writing the module under test.
    """
    import importlib

    try:
        return importlib.import_module(dotted)
    except Exception:
        return None


class TestData:
    """Deterministic pseudo-random bytes (upstream TestData.Bytes)."""

    @staticmethod
    def bytes(length: int, seed: int = 1) -> bytes:
        return random.Random(seed).randbytes(length)


class TempFolder:
    """Creates and cleans up a temporary folder (upstream TempFolder).

    Use as ``with TempFolder() as dest:`` then ``dest.path``; or plain
    ``dest = TempFolder()`` + ``dest.cleanup()``.
    """

    def __init__(self) -> None:
        self.path = os.path.join(tempfile.gettempdir(), "apt-tests", uuid.uuid4().hex)
        os.makedirs(self.path, exist_ok=True)

    def __enter__(self) -> "TempFolder":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class FakeStream(io.BytesIO):
    """BytesIO that mimics MTP chunking: at most 64 KiB per read.

    Calls the phone's before_read hook (path, position) before every read
    and raises DeviceDisconnectedError once the phone is unplugged.
    """

    _CHUNK = 64 * 1024

    def __init__(self, phone: "FakeAdapter", path: str, content: bytes) -> None:
        super().__init__(content)
        self._phone = phone
        self._path = path

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        self._phone._throw_if_disconnected()
        if self._phone.before_read is not None:
            self._phone.before_read(self._path, self.tell())
        if size is None or size < 0:
            size = self._CHUNK
        return super().read(min(size, self._CHUNK))

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


class MemoryHistory:
    """In-memory stand-in for HistoryDatabase (upstream MemoryHistory).

    Implements the CONTRACT record(device_key, source_path, size) shape,
    but also tolerates the upstream record(device_key, item, destination)
    call shape so transfer tests survive either builder choice.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str, int]] = []

    def record(self, device_key, source_path, size=None, destination=None) -> None:
        if hasattr(source_path, "source_path"):  # upstream-style MediaItem
            size = source_path.size
            source_path = source_path.source_path
        key = (device_key, str(source_path), int(size))  # type: ignore[arg-type]
        if key not in self.records:
            self.records.append(key)

    def get_transferred_keys(self, device_key: str) -> set[str]:
        return {f"{path}|{size}" for dev, path, size in self.records if dev == device_key}

    def close(self) -> None:
        pass


class FakeAdapter(DeviceAdapter):
    """An in-memory phone implementing DeviceAdapter (upstream FakePhone).

    Device paths use "/" and start at ROOT ("Internal shared storage"),
    e.g. "Internal shared storage/DCIM/Camera/IMG_0001.jpg".

    Hooks for failure simulation (mirror upstream):
      - ``connected``: set False to simulate an unplugged phone.
      - ``before_read``: called before every chunk read with (path, position);
        may raise to simulate glitches.
      - ``truncated``: paths whose stream returns half the reported bytes.
      - ``opened_by``: records "id" or "path" per open_read call.
    """

    ROOT = "Internal shared storage"

    def __init__(self) -> None:
        self._folders: dict[str, list[DeviceEntry]] = {self.ROOT: []}
        self._files: dict[str, bytes] = {}
        self._path_by_id: dict[str, str] = {}
        self._thumbnails: dict[str, bytes] = {}
        self._next_id = 1
        self._info = PhoneInfo("fake-device-id", "Fake Phone", "SN:FAKE")
        self.connected = True
        self.before_read: Callable[[str, int], None] | None = None
        self.truncated: set[str] = set()
        self.opened_by: list[str] = []
        self._closed = False

    # -- tree building -----------------------------------------------------

    def add_folder(self, relative_path: str, hidden: bool = False) -> str:
        """Create a folder (and parents); returns its device path.

        Only the LAST segment gets the hidden flag; parents are hidden
        only when their own name starts with a dot.
        """
        parts = [p for p in relative_path.strip("/").split("/") if p]
        folder_path = self.ROOT
        for i, part in enumerate(parts):
            child = f"{folder_path}/{part}"
            if child not in self._folders:
                is_hidden = (hidden and i == len(parts) - 1) or part.startswith(".")
                self._folders[child] = []
                self._folders[folder_path].append(
                    DeviceEntry(child, part, True, is_hidden, 0, None, None)
                )
            folder_path = child
        return folder_path

    def add_file(self, relative_path: str, content: bytes,
                 modified: datetime | None = None) -> MediaItem:
        """Add a file under ROOT; returns the MediaItem for it."""
        parts = [p for p in relative_path.strip("/").split("/") if p]
        folder_path = self.ROOT
        for part in parts[:-1]:
            child = f"{folder_path}/{part}"
            if child not in self._folders:
                self._folders[child] = []
                self._folders[folder_path].append(
                    DeviceEntry(child, part, True, part.startswith("."), 0, None, None)
                )
            folder_path = child
        name = parts[-1]
        path = f"{folder_path}/{name}"
        date = modified or datetime(2026, 10, 1, 12, 0, 0)
        pid = f"PUID-{self._next_id}"
        self._next_id += 1
        self._files[path] = content
        self._path_by_id[pid] = path
        # Replace any previous entry with the same name (re-adding a file).
        entries = self._folders[folder_path]
        entries[:] = [e for e in entries if e.name != name]
        entries.append(DeviceEntry(path, name, False, name.startswith("."), len(content), date, date, pid))
        folder = "/".join(parts[:-1])
        kind, file_category = _classify(name)
        return MediaItem(
            source_path=path,
            name=name,
            size=len(content),
            modified=date,
            created=date,
            folder=folder,
            folder_category=_folder_category(folder),
            kind=kind,
            file_category=file_category,
            persistent_id=pid,
            device_key=self._info.device_id,
        )

    def set_thumbnail(self, path: str, data: bytes) -> None:
        self._thumbnails[path] = data

    def file_count(self) -> int:
        return len(self._files)

    # -- DeviceAdapter -----------------------------------------------------

    @property
    def info(self) -> PhoneInfo:
        return self._info

    @property
    def is_connected(self) -> bool:
        return self.connected and not self._closed

    def get_storage_roots(self) -> list[StorageRoot]:
        return [StorageRoot(self.ROOT, "Internal shared storage", 0, 0)]

    def folder_has_entries(self, path: str) -> bool:
        entries = self._folders.get(path)
        return bool(entries)

    def list_folder(self, path: str, on_batch: Callable[[list[DeviceEntry]], None]) -> None:
        self._throw_if_disconnected()
        entries = self._folders.get(path, [])
        for i in range(0, len(entries), 200):
            on_batch(list(entries[i:i + 200]))

    def open_read(self, path: str, persistent_id: str | None) -> FakeStream:
        self._throw_if_disconnected()
        if persistent_id is not None and persistent_id in self._path_by_id:
            resolved = self._path_by_id[persistent_id]
            self.opened_by.append("id")
        else:
            resolved = path
            self.opened_by.append("path")
        if resolved not in self._files:
            raise FileNotFoundError(f"No such file on phone: {resolved}")
        content = self._files[resolved]
        if resolved in self.truncated:
            content = content[: len(content) // 2]
        return FakeStream(self, resolved, content)

    def get_thumbnail(self, path: str, persistent_id: str | None) -> bytes | None:
        self._throw_if_disconnected()
        resolved = self._resolve(path, persistent_id)
        return self._thumbnails.get(resolved)

    def read_all_bytes(self, path: str, persistent_id: str | None, max_bytes: int) -> bytes:
        self._throw_if_disconnected()
        resolved = self._resolve(path, persistent_id)
        if resolved not in self._files:
            raise FileNotFoundError(f"No such file on phone: {resolved}")
        content = self._files[resolved]
        if len(content) > max_bytes:
            raise ValueError(f"File is {len(content)} bytes, over the {max_bytes} byte cap")
        return content

    def delete_file(self, path: str) -> None:
        self._throw_if_disconnected()
        if path not in self._files:
            raise FileNotFoundError(f"No such file on phone: {path}")
        del self._files[path]
        self._path_by_id = {k: v for k, v in self._path_by_id.items() if v != path}
        parent = path.rsplit("/", 1)[0]
        if parent in self._folders:
            self._folders[parent][:] = [e for e in self._folders[parent] if e.full_name != path]

    def is_still_connected(self) -> bool:
        return self.connected and not self._closed

    def close(self) -> None:
        self._closed = True

    # -- helpers -----------------------------------------------------------

    def _resolve(self, path: str, persistent_id: str | None) -> str:
        if persistent_id is not None and persistent_id in self._path_by_id:
            return self._path_by_id[persistent_id]
        return path

    def _throw_if_disconnected(self) -> None:
        if not self.connected or self._closed:
            raise DeviceDisconnectedError("Device not connected (simulated)")
