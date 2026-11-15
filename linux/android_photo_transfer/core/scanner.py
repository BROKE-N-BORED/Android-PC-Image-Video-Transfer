"""Media scanning for the Android Photo Transfer Linux port.

Ports upstream Core/Media/MediaScanner.cs + MediaQuery.cs + MediaItem.cs.

scan() walks every storage root breadth-first, priority folders first
(DCIM, Pictures, Download, Movies, Documents) so the photos people care
about appear quickly. Only hidden/dot-folders are skipped: Android/data
and Android/obb are INCLUDED (user order, v1.0.4).

NOTE: Android 11+ phones do not expose Android/data over MTP at all, so
on those phones that folder simply will not appear in the listing. That
is a restriction of the phone, not of this app; nothing here filters it
out.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..device.interface import DeviceAdapter
from ..device.types import DeviceDisconnectedError, DeviceEntry
from . import classifier

logger = logging.getLogger("android_photo_transfer")

PRIORITY_FOLDERS = ("dcim", "pictures", "download", "movies", "documents")


@dataclass
class MediaItem:
    """Lightweight metadata for one file on the phone. Never holds contents."""

    source_path: str  # device path; also the identity of the item
    name: str
    size: int
    modified: datetime | None
    created: datetime | None
    folder: str  # folder relative to its storage root, using "/", e.g. "DCIM/Camera"
    folder_category: str = ""
    kind: str = ""  # "photo" | "video" | "file"
    file_category: str = ""
    persistent_id: str | None = None
    device_key: str = ""
    already_transferred: bool = False

    def __post_init__(self) -> None:
        if not self.folder_category:
            self.folder_category = classifier.folder_category(self.folder)
        if not self.kind:
            if classifier.is_photo(self.name):
                self.kind = "photo"
            elif classifier.is_video(self.name):
                self.kind = "video"
            else:
                self.kind = "file"
        if not self.file_category:
            self.file_category = classifier.file_category(self.name)

    @property
    def extension(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def date(self) -> datetime | None:
        """Best available capture date. Android reports capture time as modified time."""
        return self.modified if self.modified is not None else self.created

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    @property
    def is_media(self) -> bool:
        return self.kind != "file"

    @property
    def history_key(self) -> str:
        """Key the transfer history uses to recognise this exact file again."""
        return f"{self.source_path}|{self.size}"


def _priority(root_folder: str) -> int:
    """Sort key for root-level folders: priority folders first, Android last."""
    name = root_folder.lower()
    if name in PRIORITY_FOLDERS:
        return PRIORITY_FOLDERS.index(name)
    if name == "android":
        return 1 << 30
    return len(PRIORITY_FOLDERS)


def _should_skip_folder(entry: DeviceEntry) -> bool:
    return entry.name.startswith(".") or entry.is_hidden


def _still_connected(adapter: DeviceAdapter) -> bool:
    try:
        return adapter.is_still_connected()
    except Exception:
        return False


def scan(
    adapter: DeviceAdapter,
    device_key: str,
    on_batch: Callable[[list[MediaItem]], None],
    should_stop: Callable[[], bool],
) -> None:
    """Breadth-first scan of every storage root, streaming batches via on_batch.

    Unreadable folders are logged and skipped, never fatal. If the phone
    goes away mid-scan, DeviceDisconnectedError is raised. should_stop is
    checked between folders; stopping returns normally with what was found.
    """
    roots = adapter.get_storage_roots()
    for root in roots:
        if should_stop():
            return
        pending: deque[tuple[str, str]] = deque([(root.path, "")])
        while pending:
            if should_stop():
                return
            folder_path, relative = pending.popleft()
            subfolders: list[tuple[str, str]] = []
            try:
                def _handle(entries: list[DeviceEntry]) -> None:
                    found: list[MediaItem] = []
                    for entry in entries:
                        if entry.is_folder:
                            child_relative = (
                                entry.name
                                if not relative
                                else f"{relative}/{entry.name}"
                            )
                            if not _should_skip_folder(entry):
                                subfolders.append((entry.full_name, child_relative))
                            continue
                        # Every visible file is collected: photos/videos feed
                        # the Photos tab, everything feeds the Files tab.
                        if entry.name.startswith(".") or entry.is_hidden:
                            continue
                        found.append(
                            MediaItem(
                                source_path=entry.full_name,
                                name=entry.name,
                                size=entry.size,
                                modified=entry.modified,
                                created=entry.created,
                                folder=relative,
                                persistent_id=entry.persistent_id,
                                device_key=device_key,
                            )
                        )
                    if found:
                        on_batch(found)

                adapter.list_folder(folder_path, _handle)
            except DeviceDisconnectedError:
                raise
            except Exception as exc:
                if not _still_connected(adapter):
                    raise DeviceDisconnectedError(
                        "The phone is no longer connected."
                    ) from exc
                logger.warning(
                    "Skipped unreadable folder '%s' | %s: %s",
                    relative,
                    type(exc).__name__,
                    exc,
                )
            if not relative:
                subfolders.sort(key=lambda sub: _priority(sub[1]))
            pending.extend(subfolders)


class MediaQuery:
    """Filtering, search and sorting over already-collected metadata.

    Never rescans the phone. Ports upstream MediaQuery.Apply.
    """

    SHOW_ALL = "all"
    SHOW_NOT_ON_PC = "not_on_pc"
    SHOW_ON_PC = "on_pc"

    @staticmethod
    def apply(
        items: list[MediaItem],
        show: str = SHOW_ALL,
        search: str = "",
        sort_key: str = "date",
        sort_desc: bool = True,
    ) -> list[MediaItem]:
        if show == MediaQuery.SHOW_NOT_ON_PC:
            filtered = [m for m in items if not m.already_transferred]
        elif show == MediaQuery.SHOW_ON_PC:
            filtered = [m for m in items if m.already_transferred]
        else:
            filtered = list(items)

        terms = search.split() if search and search.strip() else []
        if terms:
            def _matches(item: MediaItem) -> bool:
                for term in terms:
                    lowered = term.lower()
                    if (
                        lowered not in item.name.lower()
                        and lowered not in item.folder.lower()
                        and lowered not in item.extension.lower()
                    ):
                        return False
                return True

            filtered = [m for m in filtered if _matches(m)]

        # Name is always the tiebreak (upstream ThenName), ascending, even
        # when the primary sort is descending. Sort by it first; the
        # primary sort below is stable, so ties keep name order.
        filtered.sort(key=lambda m: (m.name.lower(), m.source_path))
        if sort_key == "name":
            filtered.sort(
                key=lambda m: (m.name.lower(), m.source_path), reverse=sort_desc
            )
        elif sort_key == "size":
            filtered.sort(key=lambda m: m.size, reverse=sort_desc)
        elif sort_key == "folder":
            filtered.sort(
                key=lambda m: (m.folder.lower(), m.name.lower()), reverse=sort_desc
            )
        else:  # "date" (default, newest first) and anything unknown
            def _date_key(item: MediaItem) -> float:
                if item.date is None:
                    return float("-inf")
                return item.date.timestamp()

            filtered.sort(key=_date_key, reverse=sort_desc)
        return filtered
