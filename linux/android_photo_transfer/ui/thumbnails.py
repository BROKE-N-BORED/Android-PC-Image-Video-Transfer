"""Thumbnail loading for the Linux port.

Mirrors upstream Core/Thumbnails/ThumbnailManager.cs + ThumbnailCache.cs:
device thumbnail first, then a full-decode fallback for photos up to 30 MB,
behind an LRU memory cache (default 64 MB, clamp 16..1024 MB).

All device reads happen on a worker thread. Decoding produces QImage there;
the QPixmap conversion happens on the GUI thread (QPixmap is GUI-thread only).
Only on-screen tiles are loaded where the view supplies a visibility probe.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

log = logging.getLogger("android_photo_transfer")

FULL_DECODE_LIMIT = 30 * 1024 * 1024  # upstream ThumbnailManager.FullDecodeLimit
THUMB_WIDTH = 320  # decoded at 2x the 152 px tile image for crisp tiles
THUMB_HEIGHT = 256
DEFAULT_CACHE_MB = 64
MIN_CACHE_MB = 16
MAX_CACHE_MB = 1024


def cache_key(device_key: str, source_path: str, size: int) -> str:
    return f"{device_key}|{source_path}|{size}"


class ThumbnailCache:
    """LRU cache of QPixmaps bounded by a byte budget."""

    def __init__(self, capacity_bytes: int):
        self._capacity = max(1, capacity_bytes)
        self._items: OrderedDict[str, QPixmap] = OrderedDict()
        self._bytes = 0

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @capacity_bytes.setter
    def capacity_bytes(self, value: int) -> None:
        self._capacity = max(1, value)
        self._evict()

    @property
    def current_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str) -> QPixmap | None:
        pixmap = self._items.get(key)
        if pixmap is not None:
            self._items.move_to_end(key)
        return pixmap

    def put(self, key: str, pixmap: QPixmap) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= _pixmap_bytes(old)
        self._items[key] = pixmap
        self._bytes += _pixmap_bytes(pixmap)
        self._evict()

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def _evict(self) -> None:
        while self._items and self._bytes > self._capacity:
            _, pixmap = self._items.popitem(last=False)
            self._bytes -= _pixmap_bytes(pixmap)


def _pixmap_bytes(pixmap: QPixmap) -> int:
    # 4 bytes per pixel (ARGB32) is the honest in-memory cost.
    return max(1, pixmap.width() * pixmap.height() * 4)


class _ThumbnailWorker(QObject):
    """Runs on its own thread; pulls requests off a queue, decodes, emits."""

    ready = Signal(str, QImage)  # key, decoded image (GUI thread converts)
    failed = Signal(str)  # key

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: list[tuple] = []  # (key, source_path, persistent_id, size, kind)
        self._queued_keys: set[str] = set()
        self._device = None
        self._stopped = False

    def set_device(self, device) -> None:
        with self._lock:
            self._device = device
            self._queue.clear()
            self._queued_keys.clear()
            self._cond.notify_all()

    def request(self, key: str, source_path: str, persistent_id, size: int, kind: str) -> None:
        with self._lock:
            if key in self._queued_keys or self._stopped:
                return
            self._queued_keys.add(key)
            self._queue.append((key, source_path, persistent_id, size, kind))
            self._cond.notify_all()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._cond.notify_all()

    @Slot()
    def run(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                key, source_path, persistent_id, size, kind = self._queue.pop(0)
                self._queued_keys.discard(key)
                device = self._device
            if device is None:
                self.failed.emit(key)
                continue
            try:
                image = self._load(device, source_path, persistent_id, size, kind)
            except Exception as exc:  # never let one thumbnail kill the worker
                log.debug("Thumbnail failed for %s: %s", source_path, exc)
                image = None
            if image is None or image.isNull():
                self.failed.emit(key)
            else:
                self.ready.emit(key, image)

    @staticmethod
    def _load(device, source_path, persistent_id, size, kind) -> QImage | None:
        # 1. Device thumbnail first (cheap: the phone already made it).
        try:
            raw = device.get_thumbnail(source_path, persistent_id)
        except Exception:
            raw = None
        if raw:
            image = QImage.fromData(raw)
            if not image.isNull():
                return _scale(image)
        # 2. Full-decode fallback, photos only, at most 30 MB.
        if kind == "photo" and 0 < size <= FULL_DECODE_LIMIT:
            try:
                data = device.read_all_bytes(source_path, persistent_id, FULL_DECODE_LIMIT)
            except Exception:
                return None
            image = QImage.fromData(data)
            if not image.isNull():
                return _scale(image)
        return None


def _scale(image: QImage) -> QImage:
    from PySide6.QtCore import Qt

    return image.scaled(
        THUMB_WIDTH, THUMB_HEIGHT, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class ThumbnailManager(QObject):
    """Owns the cache + worker thread; the views ask for thumbnails by item."""

    thumbnail_ready = Signal(str)  # cache key whose pixmap is now available
    thumbnail_failed = Signal(str)

    def __init__(self, cache: ThumbnailCache):
        super().__init__()
        self.cache = cache
        self.enabled = True
        self.suspended = False  # True while a transfer runs: copying owns the phone
        self.is_on_screen = None  # optional probe: MediaItem -> bool
        self._failed_keys: set[str] = set()
        self._thread = QThread(self)
        self._thread.setObjectName("thumbnails")
        self._worker = _ThumbnailWorker()
        self._worker.moveToThread(self._thread)
        self._worker.ready.connect(self._on_ready)
        self._worker.failed.connect(self._on_failed)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def set_device(self, device) -> None:
        self._failed_keys.clear()
        self._worker.set_device(device)

    def request(self, item) -> QPixmap | None:
        """Return a cached pixmap, or queue a load and return None.

        `item` is a core MediaItem. Items that are not on screen (when the
        view supplies a probe) are not queued.
        """
        key = cache_key(item.device_key, item.source_path, item.size)
        pixmap = self.cache.get(key)
        if pixmap is not None:
            return pixmap
        if not self.enabled or self.suspended:
            return None
        if key in self._failed_keys:
            return None
        if self.is_on_screen is not None:
            try:
                if not self.is_on_screen(item):
                    return None
            except Exception:
                pass
        self._worker.request(key, item.source_path, item.persistent_id, item.size, item.kind)
        return None

    def pixmap_for(self, item) -> QPixmap | None:
        return self.cache.get(cache_key(item.device_key, item.source_path, item.size))

    @Slot(str, QImage)
    def _on_ready(self, key: str, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._on_failed(key)
            return
        self.cache.put(key, pixmap)
        self.thumbnail_ready.emit(key)

    @Slot(str)
    def _on_failed(self, key: str) -> None:
        self._failed_keys.add(key)
        self.thumbnail_failed.emit(key)

    def shutdown(self) -> None:
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(3000)
