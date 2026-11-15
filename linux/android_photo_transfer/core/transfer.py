"""The transfer engine for the Android Photo Transfer Linux port.

Ports upstream Core/Transfer/TransferManager.cs + TransferProgress.cs.
One file at a time (MTP is the bottleneck; reliability first): stream in
1 MB chunks to "name.partial", verify the byte count, atomically rename
to the final name, record history. Interrupted files are always deleted,
so a ".partial" never looks like a finished photo.

The device adapter is supplied to the constructor or as the `adapter`
keyword on run() (CONTRACT.md's run() signature has no adapter
parameter, but reads must go through adapter.open_read). Either way,
one must be provided or run() raises ValueError before copying anything.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from threading import RLock
from typing import Callable

from ..device.interface import DeviceAdapter
from ..device.types import DeviceDisconnectedError
from . import destination, errors
from .errors import InsufficientSpaceError, TransferVerificationError
from .history import HistoryDatabase
from .scanner import MediaItem

__all__ = [
    "BUFFER_SIZE",
    "FREE_SPACE_MARGIN",
    "CONSECUTIVE_FAILURE_LIMIT",
    "InsufficientSpaceError",
    "TransferVerificationError",
    "TransferItem",
    "TransferProgress",
    "TransferResult",
    "TransferManager",
]

logger = logging.getLogger("android_photo_transfer")

BUFFER_SIZE = 1024 * 1024
FREE_SPACE_MARGIN = 50 * 1024 * 1024
CONSECUTIVE_FAILURE_LIMIT = 5
RETRY_DELAY_SECONDS = 0.5
_PAUSE_POLL_SECONDS = 0.1


class _Paused(Exception):
    """Internal: the user paused mid-file; the file restarts on resume."""


class _Cancelled(Exception):
    """Internal: the user cancelled the run."""


@dataclass
class TransferItem:
    item: MediaItem
    status: str = "pending"  # pending | copying | done | skipped | failed | cancelled
    error: str | None = None
    destination_path: str | None = None


@dataclass
class TransferResult:
    disconnected: bool = False
    stopped_early: bool = False
    stop_reason: str | None = None


class TransferProgress:
    """Written by the transfer engine on every chunk, read by the UI.

    Public attributes per CONTRACT.md: copied, skipped, failed,
    current_name, current_bytes, current_total, overall_done,
    overall_total, and on_change, a callable the caller sets; the manager
    calls it after every chunk batch and item finish. files_done /
    files_total are extra, for "3 of 12" displays.
    """

    def __init__(self) -> None:
        self._gate = RLock()
        self.copied = 0
        self.skipped = 0
        self.failed = 0
        self.current_name = ""
        self.current_bytes = 0
        self.current_total = 0
        self.overall_done = 0
        self.overall_total = 0
        self.files_done = 0
        self.files_total = 0
        self.on_change: Callable[[], None] | None = None

    def _notify(self) -> None:
        callback = self.on_change
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                logger.warning("Progress callback failed | %s: %s", type(exc).__name__, exc)

    def begin(self, files: int, total_bytes: int) -> None:
        with self._gate:
            self.files_total = files
            self.overall_total = total_bytes
            self.files_done = 0
            self.overall_done = 0
            self.copied = self.skipped = self.failed = 0
            self.current_name = ""
            self.current_bytes = self.current_total = 0
        self._notify()

    def begin_file(self, item: MediaItem) -> None:
        with self._gate:
            self.current_name = item.name
            self.current_total = item.size
            self.current_bytes = 0
        self._notify()

    def add_bytes(self, count: int) -> None:
        with self._gate:
            self.current_bytes += count
            self.overall_done += count
        self._notify()

    def rollback_file(self) -> None:
        """Forget the bytes of a failed attempt before retrying the same file."""
        with self._gate:
            self.overall_done = max(0, self.overall_done - self.current_bytes)
            self.current_bytes = 0
        self._notify()

    def end_file(self, status: str) -> None:
        with self._gate:
            if status in ("pending", "copying"):
                return
            # Count the whole file as processed so the overall bar reaches
            # 100% even for skipped/failed files.
            self.overall_done += max(0, self.current_total - self.current_bytes)
            self.files_done += 1
            if status == "done":
                self.copied += 1
            elif status == "skipped":
                self.skipped += 1
            elif status == "failed":
                self.failed += 1
        self._notify()

    def undo_failed_files(self, sizes: list[int]) -> None:
        """Un-count files that were marked failed but are going back into the queue."""
        with self._gate:
            for size in sizes:
                self.failed = max(0, self.failed - 1)
                self.files_done = max(0, self.files_done - 1)
                self.overall_done = max(0, self.overall_done - size)
        self._notify()


class TransferManager:
    """Copies pending items one file at a time; see the module docstring."""

    def __init__(
        self,
        history: HistoryDatabase | None,
        adapter: DeviceAdapter | None = None,
    ):
        self._history = history
        self._adapter = adapter

    def run(
        self,
        transfer_items: list[TransferItem],
        dest_root: str,
        skip_existing: bool,
        organize_by_folder: bool,
        progress: TransferProgress,
        should_pause: Callable[[], bool],
        should_cancel: Callable[[], bool],
        adapter: DeviceAdapter | None = None,
    ) -> TransferResult:
        device = adapter or self._adapter
        if device is None:
            raise ValueError(
                "TransferManager needs a device adapter: pass one to the "
                "constructor or as run(adapter=...)."
            )
        started = time.monotonic()
        dest_root = os.path.expanduser(dest_root)
        pending = [ti for ti in transfer_items if ti.status == "pending"]

        os.makedirs(dest_root, exist_ok=True)
        self._ensure_free_space(pending, dest_root, skip_existing, organize_by_folder)

        total_bytes = sum(ti.item.size for ti in pending)
        progress.begin(len(pending), total_bytes)
        logger.info(
            "Transfer started: %d file(s), %s",
            len(pending),
            errors.format_bytes(total_bytes),
        )

        disconnected = False
        stop_reason: str | None = None
        bytes_copied = 0
        failed_in_a_row: list[TransferItem] = []

        for ti in pending:
            if should_cancel() or disconnected or stop_reason is not None:
                break
            ti.status = "copying"
            ti.error = None
            progress.begin_file(ti.item)
            try:
                self._transfer_with_pause(
                    device, ti, dest_root, skip_existing, organize_by_folder,
                    progress, should_pause, should_cancel,
                )
                if ti.status == "done":
                    bytes_copied += ti.item.size
                failed_in_a_row.clear()
            except _Cancelled:
                ti.status = "cancelled"
            except Exception as exc:
                if errors.is_destination_fatal(exc):
                    logger.error(
                        "Transfer stopped: destination problem while copying %s",
                        ti.item.name,
                        exc_info=exc,
                    )
                    self._fail(ti, exc)
                    stop_reason = errors.describe(exc)
                elif isinstance(exc, DeviceDisconnectedError) or not _still_connected(device):
                    logger.warning(
                        "Phone disconnected while copying %s | %s: %s",
                        ti.item.name, type(exc).__name__, exc,
                    )
                    progress.rollback_file()
                    ti.status = "pending"
                    disconnected = True
                else:
                    logger.error(
                        "Transfer failed: %s", ti.item.name, exc_info=exc
                    )
                    self._fail(ti, exc)
                    failed_in_a_row.append(ti)
                    if len(failed_in_a_row) >= CONSECUTIVE_FAILURE_LIMIT:
                        # The phone is answering but every file fails: that
                        # is a phone/connection problem. Put these back in
                        # the queue so Resume retries them after a reconnect.
                        logger.warning(
                            "Stopping: %d files failed in a row",
                            len(failed_in_a_row),
                        )
                        for failed in failed_in_a_row:
                            failed.status = "pending"
                            failed.error = None
                        # Earlier ones were already counted as failed; the
                        # current one has not been counted yet.
                        progress.undo_failed_files(
                            [f.item.size for f in failed_in_a_row[:-1]]
                        )
                        progress.rollback_file()
                        stop_reason = errors.PHONE_STOPPED_RESUME
                        break
            progress.end_file(ti.status)

        if should_cancel():
            for ti in pending:
                if ti.status == "pending":
                    ti.status = "cancelled"

        elapsed = time.monotonic() - started
        copied = sum(1 for ti in pending if ti.status == "done")
        skipped = sum(1 for ti in pending if ti.status == "skipped")
        failed = sum(1 for ti in pending if ti.status == "failed")
        cancelled = sum(1 for ti in pending if ti.status == "cancelled")
        remaining = sum(1 for ti in pending if ti.status == "pending")
        logger.info(
            "Transfer finished in %.1fs: %d copied, %d skipped, %d failed, "
            "%d cancelled, %d remaining%s%s",
            elapsed, copied, skipped, failed, cancelled, remaining,
            " (phone disconnected)" if disconnected else "",
            f" (stopped: {stop_reason})" if stop_reason else "",
        )
        return TransferResult(
            disconnected=disconnected,
            stopped_early=stop_reason is not None,
            stop_reason=stop_reason,
        )

    def _ensure_free_space(
        self,
        pending: list[TransferItem],
        dest_root: str,
        skip_existing: bool,
        organize_by_folder: bool,
    ) -> None:
        needed = 0
        for ti in pending:
            if skip_existing:
                folder = destination.destination_folder_for(
                    dest_root, ti.item, organize_by_folder
                )
                exact = os.path.join(
                    folder, destination.sanitize_file_name(ti.item.name)
                )
                if destination.is_already_there(exact, ti.item.size):
                    continue
            needed += ti.item.size
        available = _available_free_space(dest_root)
        if available is not None and needed + FREE_SPACE_MARGIN > available:
            raise InsufficientSpaceError(needed, available)

    def _transfer_with_pause(
        self,
        device: DeviceAdapter,
        ti: TransferItem,
        dest_root: str,
        skip_existing: bool,
        organize_by_folder: bool,
        progress: TransferProgress,
        should_pause: Callable[[], bool],
        should_cancel: Callable[[], bool],
    ) -> None:
        """Waits while paused. A file paused mid-copy is discarded and
        starts again from the beginning on resume (the phone never sits
        with an open transfer)."""
        while True:
            if should_pause():
                logger.info("Transfer paused")
                while should_pause():
                    if should_cancel():
                        raise _Cancelled
                    time.sleep(_PAUSE_POLL_SECONDS)
                logger.info("Transfer resumed")
            try:
                self._transfer_with_retry(
                    device, ti, dest_root, skip_existing, organize_by_folder,
                    progress, should_pause, should_cancel,
                )
                return
            except _Paused:
                progress.rollback_file()
                ti.status = "copying"
                logger.info(
                    "Paused while copying %s; it will restart when resumed",
                    ti.item.name,
                )

    def _transfer_with_retry(
        self,
        device: DeviceAdapter,
        ti: TransferItem,
        dest_root: str,
        skip_existing: bool,
        organize_by_folder: bool,
        progress: TransferProgress,
        should_pause: Callable[[], bool],
        should_cancel: Callable[[], bool],
    ) -> None:
        try:
            self._transfer_one(
                device, ti, dest_root, skip_existing, organize_by_folder,
                progress, should_pause, should_cancel,
            )
        except (_Paused, _Cancelled, DeviceDisconnectedError):
            raise
        except Exception as exc:
            if errors.is_destination_fatal(exc):
                raise
            if not _still_connected(device):
                raise DeviceDisconnectedError(
                    "The phone is no longer connected."
                ) from exc
            logger.warning(
                "Retrying %s after a failed attempt | %s: %s",
                ti.item.name, type(exc).__name__, exc,
            )
            progress.rollback_file()
            time.sleep(RETRY_DELAY_SECONDS)
            self._transfer_one(
                device, ti, dest_root, skip_existing, organize_by_folder,
                progress, should_pause, should_cancel,
            )

    def _transfer_one(
        self,
        device: DeviceAdapter,
        ti: TransferItem,
        dest_root: str,
        skip_existing: bool,
        organize_by_folder: bool,
        progress: TransferProgress,
        should_pause: Callable[[], bool],
        should_cancel: Callable[[], bool],
    ) -> None:
        item = ti.item
        folder = destination.destination_folder_for(
            dest_root, item, organize_by_folder
        )
        os.makedirs(folder, exist_ok=True)

        final_path = destination.resolve(
            dest_root, item, organize_by_folder, skip_existing=skip_existing
        )
        if skip_existing and destination.is_already_there(final_path, item.size):
            ti.status = "skipped"
            ti.destination_path = final_path
            self._record_history(ti)
            return

        partial_path = final_path + ".partial"
        written = 0
        try:
            with device.open_read(item.source_path, item.persistent_id) as source:
                with open(partial_path, "wb") as target:
                    while True:
                        if should_cancel():
                            raise _Cancelled
                        if should_pause():
                            raise _Paused
                        chunk = source.read(BUFFER_SIZE)
                        if not chunk:
                            break
                        target.write(chunk)
                        written += len(chunk)
                        progress.add_bytes(len(chunk))
                    target.flush()
                    os.fsync(target.fileno())

            if item.size > 0 and written != item.size:
                raise TransferVerificationError(
                    f"Size mismatch for {item.name}: expected {item.size} "
                    f"bytes, wrote {written}."
                )

            # Belt and braces: resolve() picked a free name, but if something
            # appeared there while we were copying, pick a fresh one instead
            # of overwriting it.
            if os.path.exists(final_path):
                final_path = destination.resolve(
                    dest_root, item, organize_by_folder, skip_existing=False
                )
            os.replace(partial_path, final_path)
        except BaseException:
            # Never leave a half-written file behind.
            _try_delete(partial_path)
            raise

        _apply_timestamps(final_path, item)
        ti.status = "done"
        ti.destination_path = final_path
        self._record_history(ti)

    def _record_history(self, ti: TransferItem) -> None:
        if self._history is None:
            return
        try:
            self._history.record(
                ti.item.device_key, ti.item.source_path, ti.item.size
            )
        except Exception as exc:
            logger.warning(
                "Could not record transfer history | %s: %s",
                type(exc).__name__, exc,
            )

    @staticmethod
    def _fail(ti: TransferItem, exc: Exception) -> None:
        ti.status = "failed"
        ti.error = errors.describe(exc)


def _still_connected(device: DeviceAdapter) -> bool:
    try:
        return device.is_still_connected()
    except Exception:
        return False


def _available_free_space(folder: str) -> int | None:
    """Free bytes available on the volume holding folder (walks up to an
    existing ancestor). None when it cannot be determined."""
    try:
        existing = os.path.abspath(folder)
        while not os.path.isdir(existing):
            parent = os.path.dirname(existing)
            if parent == existing:
                return None
            existing = parent
        return shutil.disk_usage(existing).free
    except Exception:
        return None


def _try_delete(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.warning(
            "Could not delete temporary file | %s: %s", type(exc).__name__, exc
        )


def _apply_timestamps(path: str, item: MediaItem) -> None:
    """Keep the file's original date so it sorts correctly in photo apps."""
    try:
        modified = item.modified if item.modified is not None else item.created
        created = item.created if item.created is not None else item.modified
        if modified is None:
            return
        atime = created.timestamp() if created is not None else modified.timestamp()
        os.utime(path, (atime, modified.timestamp()))
    except Exception as exc:
        logger.warning(
            "Could not set file dates | %s: %s", type(exc).__name__, exc
        )
