"""Qt view models for the Linux port, mirroring the C# view models.

MainViewModel owns the phone connection, the scan, the destination folder
and the transfer panel. LibraryViewModel is one tab (Photos and Videos, or
Files). TransferViewModel drives the transfer progress panel.

All device/transfer work runs on QThreads; the UI is updated via signals
only. Visible strings avoid bare ``&`` and em-dashes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ..core.errors import describe as describe_error
from ..core.scanner import MediaItem, MediaQuery, scan as scan_device
from ..core.transfer import (
    InsufficientSpaceError,
    TransferItem,
    TransferProgress,
    TransferResult,
)
from ..device.types import ConnectionState
from .helpers import format_bytes, format_date, format_duration

log = logging.getLogger("android_photo_transfer")

FLUSH_INTERVAL_MS = 400
SEARCH_DEBOUNCE_MS = 250
PROGRESS_INTERVAL_MS = 400


# ---------------------------------------------------------------------------
# One tile / row
# ---------------------------------------------------------------------------

class MediaItemVM(QObject):
    """One item in a library view. Wraps a core MediaItem."""

    checked_changed = Signal(bool)
    transferred_changed = Signal(bool)

    def __init__(self, item: MediaItem, on_checked_changed):
        super().__init__()
        self.item = item
        self._on_checked_changed = on_checked_changed
        self._is_checked = False
        self._already_transferred = bool(item.already_transferred)

    @property
    def is_checked(self) -> bool:
        return self._is_checked

    @is_checked.setter
    def is_checked(self, value: bool) -> None:
        value = bool(value)
        if value == self._is_checked:
            return
        self._is_checked = value
        self._on_checked_changed(self, value)
        self.checked_changed.emit(value)

    @property
    def already_transferred(self) -> bool:
        return self._already_transferred

    @already_transferred.setter
    def already_transferred(self, value: bool) -> None:
        value = bool(value)
        if value == self._already_transferred:
            return
        self._already_transferred = value
        self.transferred_changed.emit(value)

    # -- display helpers ---------------------------------------------------
    @property
    def name(self) -> str:
        return self.item.name

    @property
    def size_text(self) -> str:
        return format_bytes(self.item.size)

    @property
    def date_text(self) -> str:
        return format_date(self.item.modified or self.item.created)

    @property
    def folder_text(self) -> str:
        return self.item.folder or "Phone storage"

    @property
    def kind_text(self) -> str:
        mapping = {
            "Photos": "Photo", "Videos": "Video", "Documents": "Document",
            "Audio": "Audio", "Archives": "Archive", "Apps": "App",
        }
        if self.item.file_category in mapping:
            return mapping[self.item.file_category]
        ext = self.item.name.rsplit(".", 1)[-1] if "." in self.item.name else ""
        return f"{ext.upper()} file" if ext else "File"

    @property
    def is_video(self) -> bool:
        return self.item.kind == "video"

    @property
    def tooltip_text(self) -> str:
        return f"{self.name}\n{self.size_text}  •  {self.date_text}\n{self.folder_text}"

    def history_key(self) -> str:
        return f"{self.item.source_path}|{self.item.size}"


# ---------------------------------------------------------------------------
# Sidebar categories and folder tree
# ---------------------------------------------------------------------------

@dataclass
class Category:
    name: str
    predicate: object = None  # Callable[[MediaItem], bool] | None (None = all)
    count: int = 0

    def matches(self, item: MediaItem) -> bool:
        return self.predicate is None or self.predicate(item)


def _in_folder(item: MediaItem, *top_folders: str) -> bool:
    first = item.folder.strip("/").split("/", 1)[0].lower()
    return first in [f.lower() for f in top_folders]


MEDIA_CATEGORIES = [
    ("All Photos and Videos", None),
    ("Camera", lambda m: m.folder_category == "Camera"),
    ("Screenshots", lambda m: m.folder_category == "Screenshots"),
    ("Pictures", lambda m: m.folder_category == "Pictures"),
    ("Downloads", lambda m: m.folder_category == "Downloads"),
    ("Videos", lambda m: m.kind == "video"),
    ("Other Media", lambda m: m.folder_category == "Other"),
]

FILES_CATEGORIES = [
    ("All Files", None),
    ("Documents", lambda m: m.file_category == "Documents"),
    ("Music and Audio", lambda m: m.file_category == "Audio"),
    ("Archives (ZIP, RAR)", lambda m: m.file_category == "Archives"),
    ("Apps (APK)", lambda m: m.file_category == "Apps"),
    ("Other Files", lambda m: m.file_category == "Other"),
]

QUICK_ACCESS = [
    ("Download", lambda m: _in_folder(m, "download", "downloads")),
    ("Documents", lambda m: _in_folder(m, "documents")),
    ("Music", lambda m: _in_folder(m, "music")),
    ("Recordings", lambda m: _in_folder(m, "recordings", "sounds", "call", "voice recorder", "recorder")),
    ("WhatsApp", lambda m: "whatsapp" in m.folder.lower()),
    ("Bluetooth", lambda m: _in_folder(m, "bluetooth")),
]


class TreeNode:
    """One node of the phone folder tree."""

    def __init__(self, name: str, folder: str, parent: "TreeNode | None" = None):
        self.name = name
        self.folder = folder  # full phone folder path ("" for the root node)
        self.parent = parent
        self.children: dict[str, TreeNode] = {}
        self.count = 0

    def get_or_add_child(self, segment: str) -> "TreeNode":
        child = self.children.get(segment)
        if child is None:
            folder = f"{self.folder}/{segment}" if self.folder else segment
            child = TreeNode(segment, folder, self)
            self.children[segment] = child
        return child

    def contains(self, item: MediaItem) -> bool:
        folder = item.folder.strip("/")
        if not self.folder:
            return folder == ""
        return folder == self.folder or folder.startswith(self.folder + "/")


# Sort modes: (label, MediaQuery sort_key, sort_desc)
SORT_OPTIONS = [
    ("Newest First", "date", True),
    ("Oldest First", "date", False),
    ("Name A-Z", "name", False),
    ("Name Z-A", "name", True),
    ("Largest First", "size", True),
    ("Smallest First", "size", False),
]

TYPE_OPTIONS = [("Photos + Videos", "all"), ("Photos", "photo"), ("Videos", "video")]
SHOW_OPTIONS = [("Show all", "all"), ("Not on PC yet", "not_on_pc"), ("Already on PC", "on_pc")]


# ---------------------------------------------------------------------------
# One tab
# ---------------------------------------------------------------------------

class LibraryViewModel(QObject):
    """One tab: its own sidebar, search/sort/filters, grid/list and selection."""

    view_changed = Signal()       # visible items / title / empty panel changed
    counts_changed = Signal()     # category counts / item counts changed
    selection_changed = Signal()  # selection text / button state changed
    state_changed = Signal()      # can_* flags / empty panel changed

    def __init__(self, kind, settings_manager, thumbnails, start_transfer,
                 open_preview, selection_changed_cb, delete_items):
        super().__init__()
        self.kind = kind  # "media" or "files"
        self.is_media = kind == "media"
        self._settings_manager = settings_manager
        self._thumbnails = thumbnails
        self._start_transfer = start_transfer
        self._open_preview = open_preview
        self._selection_changed_cb = selection_changed_cb
        self._delete_items = delete_items

        self.header = "Photos and Videos" if self.is_media else "Files"
        self.item_noun = "photos and videos" if self.is_media else "files"
        if self.is_media:
            self.categories = [Category(n, p) for n, p in MEDIA_CATEGORIES]
            self.quick_access: list[Category] = []
        else:
            self.categories = [Category(n, p) for n, p in FILES_CATEGORIES]
            self.quick_access = [Category(n, p) for n, p in QUICK_ACCESS]

        self._all_items: list[MediaItemVM] = []
        self._items_by_path: dict[str, MediaItemVM] = {}
        self._storage_nodes: dict[str, TreeNode] = {}
        self.folder_roots: list[TreeNode] = []
        self.visible_items: list[MediaItemVM] = []

        self.selected_category: Category | None = self.categories[0]
        self.selected_folder: TreeNode | None = None
        self.view_title = ""
        self.search_text = ""
        self.sort_index = 0
        self.type_filter = "all"
        self.transfer_filter = "all"
        self.is_grid_view = self.is_media

        self.selected_count = 0
        self.selected_transferred_count = 0
        self.new_items_count = 0
        self._selected_bytes = 0
        self._selection_anchor: MediaItemVM | None = None
        self._selection_batch = False

        self.can_transfer_selected = False
        self.can_transfer_all = False
        self.can_import_new = False
        self.can_select = False
        self.can_delete = False
        self.show_empty_panel = False
        self.empty_message = ""

        self._ready = False
        self._idle = True
        self._scanning = False
        self._scan_error: str | None = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self.refresh_view)

        self._update_selection_text()
        self.refresh_view()

    # -- properties ---------------------------------------------------------
    @property
    def item_count(self) -> int:
        return len(self._all_items)

    @property
    def selection_text(self) -> str:
        return self._selection_text

    @property
    def transfer_button_text(self) -> str:
        return f"TRANSFER {self.selected_count}" if self.selected_count > 0 else "TRANSFER"

    @property
    def untick_transferred_text(self) -> str:
        return f"Untick already on PC ({self.selected_transferred_count})"

    @property
    def import_new_text(self) -> str:
        return f"Import New ({self.new_items_count})" if self.new_items_count > 0 else "Import New"

    def accepts(self, item: MediaItem) -> bool:
        if self.is_media:
            return item.kind in ("photo", "video")
        return item.kind == "file"

    # -- items (fed by the main view model as the scan finds them) ----------
    def add_items(self, items, transferred_keys: set[str]) -> None:
        categories = self.categories + self.quick_access
        counts = [0] * len(categories)
        added = False
        for item in items:
            if not self.accepts(item) or item.source_path in self._items_by_path:
                continue
            # MediaQuery filters on the item's flag, so set it on the item
            # itself, not just on the view model wrapper.
            item.already_transferred = f"{item.source_path}|{item.size}" in transferred_keys
            vm = MediaItemVM(item, self._on_item_checked_changed)
            vm.already_transferred = item.already_transferred
            self._all_items.append(vm)
            self._items_by_path[item.source_path] = vm
            self._add_to_folder_tree(item)
            for i, cat in enumerate(categories):
                if cat.matches(item):
                    counts[i] += 1
            added = True
        if not added:
            return
        for i, cat in enumerate(categories):
            cat.count += counts[i]
        self.new_items_count = sum(1 for v in self._all_items if not v.already_transferred)
        self.counts_changed.emit()

    def clear(self) -> None:
        self._all_items.clear()
        self._items_by_path.clear()
        self._storage_nodes.clear()
        self.folder_roots.clear()
        self.visible_items = []
        self._selection_anchor = None
        self._selected_bytes = 0
        self.selected_count = 0
        self.selected_transferred_count = 0
        self.new_items_count = 0
        for cat in self.categories + self.quick_access:
            cat.count = 0
        self.selected_folder = None
        self.selected_category = self.categories[0]
        self._update_selection_text()
        self.refresh_view()
        self.counts_changed.emit()

    def mark_transferred(self, paths) -> None:
        done = [self._items_by_path[p] for p in paths if p in self._items_by_path]
        if not done:
            return
        self._set_checked([v for v in done if v.is_checked], False)
        for vm in done:
            vm.item.already_transferred = True
            vm.already_transferred = True
        self.new_items_count = sum(1 for v in self._all_items if not v.already_transferred)
        if self.transfer_filter != "all":
            self.refresh_view()
        self._update_state()

    def remove_items(self, paths) -> None:
        removed = []
        for path in paths:
            vm = self._items_by_path.pop(path, None)
            if vm is not None:
                removed.append(vm)
        if not removed:
            return
        self._set_checked([v for v in removed if v.is_checked], False)
        gone = set(removed)
        self._all_items = [v for v in self._all_items if v not in gone]
        categories = self.categories + self.quick_access
        for vm in removed:
            for cat in categories:
                if cat.matches(vm.item):
                    cat.count = max(0, cat.count - 1)
            self._remove_from_folder_tree(vm.item)
        self.new_items_count = sum(1 for v in self._all_items if not v.already_transferred)
        self.counts_changed.emit()
        self.refresh_view()

    def _add_to_folder_tree(self, item: MediaItem) -> None:
        folder = item.folder.strip("/")
        if not folder:
            node = self._storage_nodes.get("")
            if node is None:
                node = TreeNode("Phone storage", "", None)
                self._storage_nodes[""] = node
                self.folder_roots.append(node)
            node.count += 1
            return
        segments = folder.split("/")
        root_key = segments[0]
        node = self._storage_nodes.get(root_key)
        if node is None:
            node = TreeNode(root_key, root_key, None)
            self._storage_nodes[root_key] = node
            self.folder_roots.append(node)
        node.count += 1
        for segment in segments[1:]:
            node = node.get_or_add_child(segment)
            node.count += 1

    def _remove_from_folder_tree(self, item: MediaItem) -> None:
        folder = item.folder.strip("/")
        if not folder:
            node = self._storage_nodes.get("")
            if node is not None:
                node.count = max(0, node.count - 1)
            return
        segments = folder.split("/")
        node = self._storage_nodes.get(segments[0])
        if node is None:
            return
        node.count = max(0, node.count - 1)
        for segment in segments[1:]:
            node = node.children.get(segment)
            if node is None:
                return
            node.count = max(0, node.count - 1)

    # -- environment ---------------------------------------------------------
    def set_environment(self, ready: bool, idle: bool, scanning: bool, scan_error) -> None:
        self._ready = ready
        self._idle = idle
        self._scanning = scanning
        self._scan_error = scan_error
        self._update_state()

    def _update_state(self) -> None:
        self.can_select = self._ready and len(self.visible_items) > 0
        self.can_transfer_selected = self._ready and self._idle and self.selected_count > 0
        self.can_delete = self._ready and self._idle and self.selected_count > 0
        self.can_transfer_all = self._ready and self._idle and len(self._all_items) > 0
        self.can_import_new = self._ready and self._idle and self.new_items_count > 0
        self.show_empty_panel = self._ready and len(self.visible_items) == 0
        if len(self._all_items) == 0:
            if self._scanning:
                self.empty_message = f"Looking for {self.item_noun}..."
            elif self._scan_error:
                self.empty_message = self._scan_error
            else:
                self.empty_message = f"No {self.item_noun} were found on this phone."
        elif self.transfer_filter == "not_on_pc" and not self.search_text:
            self.empty_message = "Everything here is already on this PC."
        else:
            self.empty_message = f"No {self.item_noun} match the current view."
        self.state_changed.emit()

    # -- browsing ------------------------------------------------------------
    def select_category(self, category: Category | None) -> None:
        if category is None:
            return
        self.selected_category = category
        self.selected_folder = None
        self.refresh_view()

    def select_folder(self, node: TreeNode | None) -> None:
        if node is None:
            if self.selected_category is None:
                self.selected_folder = None
                self.selected_category = self.categories[0]
            return
        self.selected_folder = node
        self.selected_category = None
        self.refresh_view()

    def set_search_text(self, text: str) -> None:
        if text == self.search_text:
            return
        self.search_text = text
        self._search_timer.stop()
        self._search_timer.start()

    def set_sort_index(self, index: int) -> None:
        if index == self.sort_index:
            return
        self.sort_index = index
        self.refresh_view()

    def set_type_filter(self, value: str) -> None:
        if value == self.type_filter:
            return
        self.type_filter = value
        self.refresh_view()

    def set_transfer_filter(self, value: str) -> None:
        if value == self.transfer_filter:
            return
        self.transfer_filter = value
        self.refresh_view()

    def set_grid_view(self, grid: bool) -> None:
        if not self.is_media:
            return
        if grid != self.is_grid_view:
            self.is_grid_view = grid
            self.view_changed.emit()

    def sort_by_column(self, column: str) -> None:
        """Clicking a column header sorts by it; clicking again reverses."""
        label, _, _ = SORT_OPTIONS[self.sort_index]
        if column == "Name":
            target = "Name Z-A" if label == "Name A-Z" else "Name A-Z"
        elif column == "Size":
            target = "Smallest First" if label == "Largest First" else "Largest First"
        elif column in ("Date", "Date modified"):
            target = "Oldest First" if label == "Newest First" else "Newest First"
        else:
            return
        for i, (name, _, _) in enumerate(SORT_OPTIONS):
            if name == target:
                self.set_sort_index(i)
                return

    def refresh_view(self) -> None:
        if self.selected_folder is not None:
            scope = self.selected_folder.contains
        elif self.selected_category is not None:
            scope = self.selected_category.matches
        else:
            scope = None
        source = self._all_items
        if scope is not None:
            source = [v for v in source if scope(v.item)]
        if self.is_media and self.type_filter in ("photo", "video"):
            source = [v for v in source if v.item.kind == self.type_filter]
        _, sort_key, sort_desc = SORT_OPTIONS[self.sort_index]
        items = [v.item for v in source]
        filtered = MediaQuery.apply(
            items, self.transfer_filter, self.search_text, sort_key, sort_desc
        )
        by_path = {v.item.source_path: v for v in source}
        self.visible_items = [by_path[i.source_path] for i in filtered if i.source_path in by_path]
        if self.selected_folder is not None:
            title = self.selected_folder.folder or self.selected_folder.name
        elif self.selected_category is not None:
            title = self.selected_category.name
        else:
            title = self.header
        self.view_title = title.upper()
        self.view_changed.emit()
        self._update_state()

    # -- selection ------------------------------------------------------------
    def _on_item_checked_changed(self, vm: MediaItemVM, is_checked: bool) -> None:
        delta = 1 if is_checked else -1
        self._selected_bytes += delta * vm.item.size
        self.selected_count += delta
        if vm.already_transferred:
            self.selected_transferred_count += delta
        if not self._selection_batch:
            self._update_selection_text()
            self._update_state()
            self._selection_changed_cb()

    def _update_selection_text(self) -> None:
        if self.selected_count == 0:
            if self.is_media:
                self._selection_text = "No photos selected: tick photos to transfer, or use Transfer All"
            else:
                self._selection_text = "No files selected: tick files to transfer, or use Transfer All"
        else:
            text = f"{self.selected_count} selected  •  {format_bytes(self._selected_bytes)}"
            if self.selected_transferred_count > 0:
                text += f"  ({self.selected_transferred_count} already on PC)"
            self._selection_text = text
        self.selection_changed.emit()

    def _set_checked(self, items, value: bool) -> None:
        self._selection_batch = True
        try:
            for vm in items:
                vm.is_checked = value
        finally:
            self._selection_batch = False
            self._update_selection_text()
            self._update_state()
            self._selection_changed_cb()

    def select_all(self) -> None:
        self._set_checked(list(self.visible_items), True)

    def clear_selection(self) -> None:
        self._set_checked([v for v in self._all_items if v.is_checked], False)

    def untick_transferred(self) -> None:
        self._set_checked(
            [v for v in self._all_items if v.is_checked and v.already_transferred], False
        )

    def on_tile_clicked(self, vm: MediaItemVM, shift: bool, double_click: bool) -> None:
        """Click toggles a tile, Shift+click ticks a range, double-click previews."""
        if double_click:
            vm.is_checked = not vm.is_checked  # undo the first click's toggle
            self.open_preview(vm)
            return
        if shift and self._selection_anchor is not None:
            try:
                start = self.visible_items.index(self._selection_anchor)
                end = self.visible_items.index(vm)
            except ValueError:
                start = end = -1
            if start >= 0 and end >= 0:
                if start > end:
                    start, end = end, start
                self._set_checked(self.visible_items[start:end + 1], True)
                self._selection_anchor = vm
                return
        vm.is_checked = not vm.is_checked
        self._selection_anchor = vm

    def toggle_checked(self, items) -> None:
        items = list(items)
        if not items:
            return
        self._set_checked(items, not all(v.is_checked for v in items))

    # -- preview / transfer / delete ------------------------------------------
    def open_preview(self, vm: MediaItemVM) -> None:
        self._open_preview(vm, list(self.visible_items))

    def transfer_item(self, vm: MediaItemVM) -> None:
        self._start_transfer([vm])

    def transfer_selected(self) -> None:
        self._start_transfer([v for v in self._all_items if v.is_checked])

    def transfer_all(self) -> None:
        self._start_transfer(list(self._all_items))

    def import_new(self) -> None:
        self._start_transfer([v for v in self._all_items if not v.already_transferred])

    def delete_selected(self) -> None:
        self._delete_items([v for v in self._all_items if v.is_checked])

    def show_folder_of(self, vm: MediaItemVM) -> TreeNode | None:
        """Return the folder-tree node for an item's folder (view expands it)."""
        folder = vm.item.folder.strip("/")
        if not folder:
            return self._storage_nodes.get("")
        segments = folder.split("/")
        node = self._storage_nodes.get(segments[0])
        if node is None:
            return None
        for segment in segments[1:]:
            node = node.children.get(segment)
            if node is None:
                return None
        return node


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

class _TransferWorker(QObject):
    finished = Signal(object)  # TransferResult
    start_failed = Signal(object)  # Exception

    def __init__(self, engine, device, jobs, dest_root, skip_existing,
                 organize_by_folder, progress, pause_event, cancel_event):
        super().__init__()
        self._engine = engine
        self._device = device
        self._jobs = jobs
        self._dest_root = dest_root
        self._skip_existing = skip_existing
        self._organize_by_folder = organize_by_folder
        self._progress = progress
        self._pause_event = pause_event
        self._cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            try:
                # Contract signature, plus the engine's adapter keyword: reads
                # must go through adapter.open_read, so the adapter is required.
                result = self._engine.run(
                    self._jobs, self._dest_root, self._skip_existing,
                    self._organize_by_folder, self._progress,
                    self._pause_event.is_set, self._cancel_event.is_set,
                    adapter=self._device,
                )
            except TypeError:
                # Fallback for an engine whose run() takes the adapter first.
                log.info("TransferManager.run rejected the contract signature; retrying with adapter first")
                result = self._engine.run(
                    self._device, self._jobs, self._dest_root, self._skip_existing,
                    self._organize_by_folder, self._progress,
                    self._pause_event.is_set, self._cancel_event.is_set,
                )
        except Exception as exc:
            self.start_failed.emit(exc)
            return
        self.finished.emit(result)


class TransferViewModel(QObject):
    """Drives the transfer progress panel and the completion panel."""

    progress_changed = Signal()
    state_changed = Signal()  # running / complete / visible changed
    finished = Signal(object, object)  # (summary dict, jobs list)
    start_failed = Signal(object)  # Exception

    def __init__(self, engine, settings_getter, current_device_getter):
        super().__init__()
        self._engine = engine
        self._settings = settings_getter
        self._current_device = current_device_getter

        self._jobs: list[TransferItem] = []
        self._options: dict | None = None
        self._device_key: str | None = None
        self._progress = TransferProgress()
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._thread: QThread | None = None
        self._worker: _TransferWorker | None = None
        self._elapsed_start = 0.0
        self._elapsed_paused_total = 0.0
        self._pause_started = 0.0
        self._last_sample_bytes = 0
        self._last_sample_time = 0.0
        self._bytes_per_second = 0.0

        self.is_visible = False
        self.is_running = False
        self.is_complete = False
        self.is_paused = False
        self.title = "Transferring"
        self.current_file = ""
        self.overall_percent = 0.0
        self.file_percent = 0.0
        self.percent_text = "0%"
        self.files_text = ""
        self.bytes_text = ""
        self.speed_text = ""
        self.elapsed_text = "00:00"
        self.remaining_text = ""
        self.pause_button_text = "Pause"
        self.live_copied = 0
        self.live_skipped = 0
        self.live_failed = 0

        self.completion_title = ""
        self.completion_summary = ""
        self.completion_counts = ""
        self.completion_detail = ""
        self.can_retry_failed = False
        self.can_resume = False
        self.resume_available = False
        self.has_failures = False
        self.show_details = False
        self.failure_details: list[str] = []

        self._timer = QTimer(self)
        self._timer.setInterval(PROGRESS_INTERVAL_MS)
        self._timer.timeout.connect(self.update_progress)

    # -- running ---------------------------------------------------------------
    def start(self, device, items, dest_root: str, skip_existing: bool,
              organize_by_folder: bool) -> None:
        if self.is_running:
            return
        self._jobs = [TransferItem(item=v.item) for v in items]
        self._options = {
            "dest_root": dest_root,
            "skip_existing": skip_existing,
            "organize_by_folder": organize_by_folder,
        }
        self._run(device)

    def _run(self, device) -> None:
        self._device_key = device.info.stable_key
        self._pause_event.clear()
        self._cancel_event.clear()
        self._progress = TransferProgress()
        self._progress.on_change = lambda: None
        self._last_sample_bytes = 0
        self._last_sample_time = 0.0
        self._bytes_per_second = 0.0

        self.title = "Transferring"
        self.current_file = "Preparing..."
        self.overall_percent = 0.0
        self.file_percent = 0.0
        self.percent_text = "0%"
        self.speed_text = ""
        self.remaining_text = ""
        self.elapsed_text = "00:00"
        self.pause_button_text = "Pause"
        self.live_copied = 0
        self.live_skipped = 0
        self.live_failed = 0
        self.is_paused = False
        self.is_complete = False
        self.show_details = False
        self.is_running = True
        self.is_visible = True
        self._elapsed_start = time.monotonic()
        self._elapsed_paused_total = 0.0
        self._timer.start()
        self.state_changed.emit()

        self._thread = QThread(self)
        self._thread.setObjectName("transfer")
        self._worker = _TransferWorker(
            self._engine, device, self._jobs,
            self._options["dest_root"], self._options["skip_existing"],
            self._options["organize_by_folder"], self._progress,
            self._pause_event, self._cancel_event,
        )
        self._worker.moveToThread(self._thread)
        self._worker.finished.connect(self._on_run_finished)
        self._worker.start_failed.connect(self._on_start_failed)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    @Slot(object)
    def _on_start_failed(self, exc: Exception) -> None:
        self._timer.stop()
        self.is_running = False
        self.is_visible = False
        self._cleanup_thread()
        log.error("Transfer could not start: %s", exc)
        self.state_changed.emit()
        self.start_failed.emit(exc)

    @Slot(object)
    def _on_run_finished(self, result: TransferResult) -> None:
        self._timer.stop()
        self.update_progress()
        self.is_running = False
        self.is_paused = False
        self._cleanup_thread()
        summary = self._build_summary(result)
        self._show_completion(summary)
        self.state_changed.emit()
        self.finished.emit(summary, list(self._jobs))

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
            self._worker = None

    def _build_summary(self, result: TransferResult) -> dict:
        counts = {"done": 0, "skipped": 0, "failed": 0, "cancelled": 0, "pending": 0}
        bytes_copied = 0
        for job in self._jobs:
            status = job.status
            if status in counts:
                counts[status] += 1
            if status == "done":
                bytes_copied += job.item.size
        return {
            "copied": counts["done"],
            "skipped": counts["skipped"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "remaining": counts["pending"],
            "bytes_copied": bytes_copied,
            "disconnected": bool(getattr(result, "disconnected", False)),
            "stopped_early": bool(getattr(result, "stopped_early", False)),
            "stop_reason": getattr(result, "stop_reason", None),
        }

    def _show_completion(self, summary: dict) -> None:
        if summary["disconnected"]:
            self.completion_title = "PHONE DISCONNECTED"
        elif summary["stopped_early"]:
            self.completion_title = "TRANSFER STOPPED"
        elif summary["cancelled"] > 0:
            self.completion_title = "TRANSFER CANCELLED"
        else:
            self.completion_title = "TRANSFER COMPLETE"

        noun = "file" if summary["copied"] == 1 else "files"
        self.completion_summary = (
            f"{summary['copied']} {noun} copied  •  {format_bytes(summary['bytes_copied'])}"
        )
        counts = [f"Skipped: {summary['skipped']}", f"Failed: {summary['failed']}"]
        if summary["cancelled"] > 0:
            counts.append(f"Not copied: {summary['cancelled']}")
        if summary["remaining"] > 0:
            counts.append(f"Remaining: {summary['remaining']}")
        self.completion_counts = "     ".join(counts)

        if summary["disconnected"]:
            self.completion_detail = (
                f"Files already copied are safe. Reconnect your phone, unlock it and choose "
                f'"File Transfer", then click Resume to copy the remaining {summary["remaining"]}.'
            )
        elif summary["stopped_early"]:
            self.completion_detail = summary.get("stop_reason") or (
                "The transfer stopped after several files in a row could not be copied. "
                "Check the phone connection, then click Resume to continue."
            )
        elif summary["skipped"] > 0:
            self.completion_detail = "Skipped files were already on this PC, so they were not copied again."
        else:
            self.completion_detail = ""

        self.failure_details = [
            f"{job.item.name}: {job.error or 'could not be copied'}"
            for job in self._jobs if job.status == "failed"
        ]
        self.has_failures = len(self.failure_details) > 0
        self.can_retry_failed = summary["failed"] > 0
        self.can_resume = any(j.status == "pending" for j in self._jobs)
        self._update_resume_availability()
        self.is_complete = True

        settings = self._settings()
        if (settings is not None and settings.open_folder_after_transfer
                and summary["copied"] > 0 and summary["failed"] == 0
                and not summary["disconnected"] and not summary["stopped_early"]
                and summary["cancelled"] == 0):
            self.open_folder()

    def update_progress(self) -> None:
        p = self._progress
        if getattr(p, "current_name", ""):
            self.current_file = p.current_name
        current_total = getattr(p, "current_total", 0) or 0
        current_bytes = getattr(p, "current_bytes", 0) or 0
        self.file_percent = min(100.0, current_bytes * 100.0 / current_total) if current_total > 0 else 0.0
        overall_total = getattr(p, "overall_total", 0) or 0
        overall_done = getattr(p, "overall_done", 0) or 0
        total_files = getattr(p, "files_total", 0) or len(self._jobs)
        files_done = getattr(p, "files_done", 0)
        if not files_done:
            files_done = getattr(p, "copied", 0) + getattr(p, "skipped", 0) + getattr(p, "failed", 0)
        if overall_total > 0:
            self.overall_percent = min(100.0, overall_done * 100.0 / overall_total)
        elif total_files > 0:
            self.overall_percent = files_done * 100.0 / total_files
        else:
            self.overall_percent = 0.0
        self.percent_text = f"{self.overall_percent:.0f}%"
        self.files_text = f"{files_done} / {total_files} files"
        self.bytes_text = f"{format_bytes(overall_done)} / {format_bytes(overall_total)}"
        self.live_copied = getattr(p, "copied", 0)
        self.live_skipped = getattr(p, "skipped", 0)
        self.live_failed = getattr(p, "failed", 0)

        now = time.monotonic()
        elapsed = now - self._elapsed_start - self._elapsed_paused_total
        if self.is_paused and self._pause_started:
            elapsed = self._pause_started - self._elapsed_start - self._elapsed_paused_total
        dt = now - self._last_sample_time if self._last_sample_time else 0
        if dt >= 0.35:
            instant = max(0, overall_done - self._last_sample_bytes) / dt
            self._bytes_per_second = instant if self._bytes_per_second <= 0 else self._bytes_per_second * 0.7 + instant * 0.3
            self._last_sample_bytes = overall_done
            self._last_sample_time = now
        self.elapsed_text = format_duration(elapsed)
        if self.is_paused:
            self.speed_text = "Paused"
            self.remaining_text = "Paused"
        else:
            self.speed_text = f"{format_bytes(int(self._bytes_per_second))}/s"
            file_fraction = files_done / total_files if total_files > 0 else 0
            byte_fraction = overall_done / overall_total if overall_total > 0 else file_fraction
            done = 0.5 * file_fraction + 0.5 * byte_fraction
            if done > 0.003 and elapsed > 8:
                self.remaining_text = "~" + format_duration(elapsed * (1 - done) / done)
            else:
                self.remaining_text = "Calculating..."
        self.progress_changed.emit()

    # -- controls --------------------------------------------------------------
    def toggle_pause(self) -> None:
        if not self.is_running:
            return
        if self.is_paused:
            self._pause_event.clear()
            if self._pause_started:
                self._elapsed_paused_total += time.monotonic() - self._pause_started
                self._pause_started = 0.0
            self.is_paused = False
            self.pause_button_text = "Pause"
            self.title = "Transferring"
        else:
            self._pause_event.set()
            self._pause_started = time.monotonic()
            self.is_paused = True
            self.pause_button_text = "Resume"
            self.title = "Paused: the current file will start over when you resume"
        self.update_progress()
        self.state_changed.emit()

    def cancel(self) -> None:
        if not self.is_running:
            return
        self.title = "Cancelling..."
        self._cancel_event.set()
        self._pause_event.clear()  # a paused run must wake up to notice the cancel
        self.state_changed.emit()

    def retry_failed(self) -> None:
        for job in self._jobs:
            if job.status == "failed":
                job.status = "pending"
                job.error = None
        self.resume()

    def resume(self) -> None:
        device = self._current_device()
        if device is None or device.info.stable_key != self._device_key or self._options is None:
            self.completion_detail = 'Connect the same phone to continue. Unlock it and choose "File Transfer" if asked.'
            self.state_changed.emit()
            return
        for job in self._jobs:
            if job.status == "cancelled":
                job.status = "pending"
        self._run(device)

    def toggle_details(self) -> None:
        self.show_details = not self.show_details
        self.state_changed.emit()

    def open_folder(self) -> None:
        from .helpers import open_folder as _open

        if self._options is not None:
            _open(self._options["dest_root"])

    def done(self) -> None:
        self.is_visible = False
        self.is_complete = False
        self.state_changed.emit()

    def on_device_changed(self, device, state) -> None:
        self._update_resume_availability(device, state)

    def _update_resume_availability(self, device=None, state=None) -> None:
        device = device if device is not None else self._current_device()
        state = state if state is not None else ConnectionState.READY
        self.resume_available = (
            device is not None
            and self._device_key is not None
            and device.info.stable_key == self._device_key
            and state == ConnectionState.READY
        )


# ---------------------------------------------------------------------------
# Workers used by the main view model
# ---------------------------------------------------------------------------

class _SerialWorker(QObject):
    """Runs blocking device calls one at a time on a single thread."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: list = []
        self._stopped = False

    def submit(self, fn) -> None:
        with self._lock:
            if self._stopped:
                return
            self._queue.append(fn)
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
                fn = self._queue.pop(0)
            try:
                fn()
            except Exception:
                log.exception("Device worker call failed")


class _ScanWorker(QObject):
    batch_ready = Signal(object)  # list[MediaItem]
    scan_finished = Signal(object)  # error string | None

    def __init__(self, adapter, device_key: str, stop_event: threading.Event):
        super().__init__()
        self._adapter = adapter
        self._device_key = device_key
        self._stop_event = stop_event

    @Slot()
    def run(self) -> None:
        error = None
        try:
            scan_device(
                self._adapter, self._device_key,
                lambda batch: self.batch_ready.emit(list(batch)),
                self._stop_event.is_set,
            )
        except Exception as exc:
            if not self._stop_event.is_set():
                log.error("Scan failed: %s", exc)
                error = describe_error(exc)
        self.scan_finished.emit(error)


class _DeleteWorker(QObject):
    delete_finished = Signal(object, object)  # (deleted paths, failed names)

    def __init__(self, adapter, paths: list[str]):
        super().__init__()
        self._adapter = adapter
        self._paths = paths

    @Slot()
    def run(self) -> None:
        deleted: list[str] = []
        failed: list[str] = []
        for path in self._paths:
            try:
                self._adapter.delete_file(path)
                deleted.append(path)
            except Exception as exc:
                log.warning("Could not delete %s: %s", path, exc)
                failed.append(path.rsplit("/", 1)[-1])
                from ..device.types import DeviceDisconnectedError

                if isinstance(exc, DeviceDisconnectedError):
                    break
        self.delete_finished.emit(deleted, failed)


# ---------------------------------------------------------------------------
# Main view model
# ---------------------------------------------------------------------------

class MainViewModel(QObject):
    """Owns the phone connection, the scan, the destination folder and transfers."""

    device_changed = Signal()      # connection state / phones / device name changed
    device_manager_changed = Signal()  # internal: DeviceManager.on_changed fired (any thread)
    status_changed = Signal()      # status bar text changed
    panels_changed = Signal()      # waiting/locked/error panels changed
    environment_changed = Signal()
    message_requested = Signal(object)  # dict: title, body, warning, offer_folder_change
    preview_requested = Signal(object, int)  # (visible MediaItemVM list, index)
    settings_requested = Signal()
    delete_dialog_requested = Signal(object)  # dict describing the delete request

    def __init__(self, settings_manager, devices, thumbnails, transfer_engine, history):
        super().__init__()
        self._settings_manager = settings_manager
        self._devices = devices
        self._thumbnails = thumbnails
        self._history = history

        self.media_library = LibraryViewModel(
            "media", settings_manager, thumbnails,
            self.start_transfer, self.open_preview,
            self._on_selection_changed, self.ask_to_delete,
        )
        self.files_library = LibraryViewModel(
            "files", settings_manager, thumbnails,
            self.start_transfer, self.open_preview,
            self._on_selection_changed, self.ask_to_delete,
        )
        self.libraries = [self.media_library, self.files_library]
        self.transfer = TransferViewModel(
            transfer_engine, lambda: self._settings_manager.current,
            lambda: self._devices.current,
        )
        self.transfer.finished.connect(self._on_transfer_finished)
        self.transfer.start_failed.connect(self._on_transfer_start_failed)
        self.transfer.state_changed.connect(self._on_transfer_state_changed)

        self.selected_tab_index = 0
        self.destination_folder = self.settings.dest_folder
        self.show_first_run = False  # contract settings carry no first-run flag

        self.connection_state = ConnectionState.WAITING
        self.device_name = "No phone connected"
        self.connection_text = "Not connected"
        self.phones = []
        self.selected_phone_id: str | None = None
        self.has_multiple_phones = False
        self.device_error_message: str | None = None
        self.is_scanning = False
        self.status_text = "Starting..."
        self.is_idle = True

        self.show_waiting_panel = True
        self.show_connecting_panel = False
        self.show_locked_panel = False
        self.show_error_panel = False

        self._active_device = None
        self._scanned_device = None
        self._scan_thread: QThread | None = None
        self._scan_worker: _ScanWorker | None = None
        self._scan_stop = threading.Event()
        self._incoming: list = []
        self._incoming_lock = threading.Lock()
        self._transferred_keys: set[str] = set()
        self._scan_error: str | None = None
        self._last_view_refresh = 0.0
        self._syncing_phone = False
        self._deleting = False

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(lambda: self._flush_incoming(force=False))

        # DeviceManager.on_changed fires on a worker thread; route it through
        # a queued signal so the sync runs on the GUI thread. This must be a
        # separate signal from device_changed: sync_device_state() emits
        # device_changed itself, so connecting that one here would loop.
        self.device_manager_changed.connect(self.sync_device_state)

        # Serial device worker: refresh / reconnect / select_phone are blocking.
        self._device_thread = QThread(self)
        self._device_thread.setObjectName("devices")
        self._device_worker = _SerialWorker()
        self._device_worker.moveToThread(self._device_thread)
        self._device_thread.started.connect(self._device_worker.run)
        self._device_thread.start()

        self._update_panels()
        self._update_environment()

    @property
    def settings(self):
        return self._settings_manager.current

    @property
    def selected_library(self) -> LibraryViewModel:
        return self.files_library if self.selected_tab_index == 1 else self.media_library

    @property
    def active_device(self):
        """The connected phone adapter, or None."""
        return self._active_device

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        log.info("Watching for phones")
        self._device_worker.submit(self._devices.refresh)

    def shutdown(self) -> None:
        self._stop_scan()
        self.transfer.cancel()
        self._flush_timer.stop()
        self._device_worker.stop()
        self._device_thread.quit()
        self._device_thread.wait(3000)

    def on_device_manager_changed(self) -> None:
        """Called (from any thread) by DeviceManager.on_changed."""
        # Route through a queued signal so the sync runs on the GUI thread.
        self.device_manager_changed.emit()

    def set_thumbnail_visibility_probe(self, probe) -> None:
        self._thumbnails.is_on_screen = probe

    def set_selected_tab(self, index: int) -> None:
        self.selected_tab_index = max(0, min(1, index))
        self._update_status()

    # -- device state --------------------------------------------------------------
    @Slot()
    def sync_device_state(self) -> None:
        state = self._devices.state
        device = self._devices.current

        self.phones = list(self._devices.phones)
        self.has_multiple_phones = len(self.phones) > 1
        self._syncing_phone = True
        self.selected_phone_id = (
            device.info.device_id if device is not None
            else (self.phones[0].device_id if self.phones else None)
        )
        self._syncing_phone = False

        if device is not self._active_device:
            self._stop_scan()
            for library in self.libraries:
                library.clear()
            self._active_device = device
            self._thumbnails.set_device(None)

        self.connection_state = state
        self.device_error_message = self._devices.error_message
        self.device_name = (
            device.info.name if device is not None
            else (self.phones[0].name if self.phones else "No phone connected")
        )
        self.connection_text = {
            ConnectionState.READY: "Connected",
            ConnectionState.CONNECTING: "Connecting...",
            ConnectionState.LOCKED: "Waiting for access",
            ConnectionState.ERROR: "Connection problem",
            ConnectionState.WAITING: "Not connected",
        }.get(state, "Not connected")

        if state == ConnectionState.READY and device is not None and device is not self._scanned_device:
            self._start_scan(device)

        self.transfer.on_device_changed(device, state)
        self._update_panels()
        self._update_environment()
        self.device_changed.emit()

    def select_phone(self, device_id: str) -> None:
        if self._syncing_phone or not device_id or self.transfer.is_running:
            return
        self._device_worker.submit(lambda: self._devices.select_phone(device_id))

    def refresh(self) -> None:
        if self.transfer.is_running:
            return
        self._device_worker.submit(self._devices.reconnect)

    def check_again(self) -> None:
        self._device_worker.submit(self._devices.refresh)

    def _update_panels(self) -> None:
        state = self.connection_state
        self.show_waiting_panel = state == ConnectionState.WAITING
        self.show_connecting_panel = state == ConnectionState.CONNECTING
        self.show_locked_panel = state == ConnectionState.LOCKED
        self.show_error_panel = state == ConnectionState.ERROR
        self.panels_changed.emit()

    def _update_environment(self) -> None:
        ready = self.connection_state == ConnectionState.READY and self._active_device is not None
        self.is_idle = not self.transfer.is_running and not self._deleting
        for library in self.libraries:
            library.set_environment(ready, self.is_idle, self.is_scanning, self._scan_error)
        self._update_status()
        self.environment_changed.emit()

    def _update_status(self) -> None:
        media = self.media_library.item_count
        files = self.files_library.item_count
        state = self.connection_state
        if state == ConnectionState.WAITING:
            text = "Waiting for a phone: connect it with a USB cable"
        elif state == ConnectionState.CONNECTING:
            text = f"Connecting to {self.device_name}..."
        elif state == ConnectionState.LOCKED:
            text = f"{self.device_name} detected: unlock it and choose File Transfer"
        elif state == ConnectionState.ERROR:
            text = self.device_error_message or "Connection problem"
        elif self.is_scanning:
            text = f"Scanning phone...  {media} photos/videos and {files} files found so far"
        elif self._scan_error is not None:
            text = f"Scan stopped: {self._scan_error}"
        else:
            text = f"Connected  •  {media} photos and videos  •  {files} files"
            new = self.selected_library.new_items_count
            if new > 0:
                text += f"  •  {new} not on this PC yet"
        if text != self.status_text:
            self.status_text = text
            self.status_changed.emit()

    def _on_selection_changed(self) -> None:
        self._update_status()

    def _on_transfer_state_changed(self) -> None:
        self._thumbnails.suspended = self.transfer.is_running
        self._update_environment()

    # -- scanning ------------------------------------------------------------------
    def _start_scan(self, device) -> None:
        self._scanned_device = device
        self._scan_stop.clear()
        self._transferred_keys = self._load_history(device.info.stable_key)
        self._thumbnails.set_device(device)
        self._scan_error = None
        self.is_scanning = True
        self._flush_timer.start()
        log.info("Scan started on %s", device.info.name)

        self._scan_thread = QThread(self)
        self._scan_thread.setObjectName("scan")
        self._scan_worker = _ScanWorker(device, device.info.stable_key, self._scan_stop)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_worker.batch_ready.connect(self._on_scan_batch)
        self._scan_worker.scan_finished.connect(self._on_scan_finished)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_thread.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.start()
        self._update_environment()

    def _load_history(self, device_key: str) -> set[str]:
        if self._history is None:
            return set()
        try:
            return set(self._history.get_transferred_keys(device_key))
        except Exception as exc:
            log.warning("Could not read transfer history: %s", exc)
            return set()

    def _stop_scan(self) -> None:
        self._scan_stop.set()
        self._scanned_device = None
        self._flush_timer.stop()
        self.is_scanning = False
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
            self._scan_thread = None
            self._scan_worker = None
        with self._incoming_lock:
            self._incoming.clear()

    @Slot(object)
    def _on_scan_batch(self, batch) -> None:
        with self._incoming_lock:
            self._incoming.extend(batch)

    @Slot(object)
    def _on_scan_finished(self, error) -> None:
        if self._scan_stop.is_set():
            return
        self._flush_timer.stop()
        self._scan_error = error
        self.is_scanning = False
        self._flush_incoming(force=True)
        log.info(
            "Scan finished: %s photos/videos, %s files",
            self.media_library.item_count, self.files_library.item_count,
        )
        # The scan thread idles in its event loop once run() returns; stop it
        # so it is not still running at shutdown.
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
            self._scan_thread = None
            self._scan_worker = None
        self._update_environment()

    def _flush_incoming(self, force: bool) -> None:
        with self._incoming_lock:
            found = self._incoming
            self._incoming = []
        if found:
            for library in self.libraries:
                library.add_items(found, self._transferred_keys)
        elif not force:
            return
        total = self.files_library.item_count
        interval = 0.4 if total < 2000 else 2.0
        now = time.monotonic()
        if force or now - self._last_view_refresh >= interval:
            for library in self.libraries:
                library.refresh_view()
            self._last_view_refresh = now
        self._update_environment()

    # -- preview / transfer ---------------------------------------------------------
    def open_preview(self, vm: MediaItemVM, visible) -> None:
        if self._active_device is None:
            return
        items = list(visible)
        try:
            index = items.index(vm)
        except ValueError:
            items = [vm]
            index = 0
        self.preview_requested.emit(items, index)

    def start_transfer(self, items) -> None:
        items = list(items)
        if self.transfer.is_running or not items:
            return
        if self._active_device is None or self.connection_state != ConnectionState.READY:
            self._show_message(
                "Phone not ready", "Connect and unlock your phone, then try again."
            )
            return
        if self.is_scanning:
            log.info("Transfer started while the scan is still running; only items found so far are included")
        if not self.destination_folder:
            return  # the view asks for a destination first via choose_destination
        self.transfer.start(
            self._active_device, items, self.destination_folder,
            self.settings.skip_existing, self.settings.organize_by_folder,
        )
        self._update_environment()

    def _on_transfer_finished(self, summary: dict, jobs) -> None:
        done = [j.item for j in jobs if j.status in ("done", "skipped")]
        for item in done:
            self._transferred_keys.add(f"{item.source_path}|{item.size}")
        paths = [i.source_path for i in done]
        for library in self.libraries:
            library.mark_transferred(paths)
        if summary.get("disconnected") or (
            summary.get("remaining", 0) > 0 and summary.get("stopped_early")
        ):
            log.info("Reconnecting to the phone after an interrupted transfer")
            self._device_worker.submit(self._devices.reconnect)
        self._update_environment()

    def _on_transfer_start_failed(self, exc: Exception) -> None:
        if isinstance(exc, InsufficientSpaceError):
            required = getattr(exc, "required", 0) or 0
            available = getattr(exc, "available", 0) or 0
            body = (
                f"Selected: {format_bytes(required)}\n"
                f"Available: {format_bytes(available)}\n"
                f"Additional space required: {format_bytes(max(0, required - available))}"
            )
            self._show_message("Not enough free space", body, offer_folder_change=True)
        else:
            from ..core.errors import is_destination_fatal

            self._show_message(
                "The transfer could not start", describe_error(exc),
                offer_folder_change=is_destination_fatal(exc),
            )

    # -- destination / settings / messages -------------------------------------------
    def choose_destination(self, parent=None) -> bool:
        from .helpers import pick_folder

        folder = pick_folder(parent, self.destination_folder, "Choose where to save your files")
        if not folder:
            return False
        self.destination_folder = folder
        self.settings.dest_folder = folder
        self._settings_manager.save()
        self.environment_changed.emit()
        return True

    def open_destination(self) -> None:
        from .helpers import open_folder

        open_folder(self.destination_folder)

    def open_settings(self) -> None:
        self.settings_requested.emit()

    def apply_settings(self) -> None:
        if not self.transfer.is_running:
            self.destination_folder = self.settings.dest_folder
        self._thumbnails.cache.capacity_bytes = (
            self.settings.thumbnail_cache_mb * 1024 * 1024
        )
        self._thumbnails.enabled = self.settings.thumbnails_enabled
        self._update_environment()

    def _show_message(self, title: str, body: str, warning=None, offer_folder_change=False) -> None:
        self.message_requested.emit({
            "title": title,
            "body": body,
            "warning": warning,
            "offer_folder_change": offer_folder_change,
        })

    # -- delete from phone (with the safety fix) ---------------------------------------
    def ask_to_delete(self, items) -> None:
        items = list(items)
        if not items or self.transfer.is_running or self._active_device is None:
            return
        on_pc = [v for v in items if v.already_transferred]
        not_on_pc = [v for v in items if not v.already_transferred]
        total_bytes = sum(v.item.size for v in items)
        self.delete_dialog_requested.emit({
            "items": items,
            "on_pc": on_pc,
            "not_on_pc": not_on_pc,
            "device_name": self.device_name,
            "total_bytes": total_bytes,
        })

    def confirm_delete(self, items, include_untransferred: bool) -> None:
        """Run the delete on a worker thread after the dialogs confirmed it."""
        items = list(items)
        if not include_untransferred:
            items = [v for v in items if v.already_transferred]
        if not items or self._active_device is None:
            return
        device = self._active_device
        paths = [v.item.source_path for v in items]
        log.info("Deleting %s item(s) from %s", len(items), device.info.name)
        self._deleting = True
        self._update_environment()

        self._delete_thread = QThread(self)
        self._delete_thread.setObjectName("delete")
        # Keep a reference: a worker held only by a signal connection can be
        # garbage-collected before its thread starts (same as the preview loader).
        self._delete_worker = _DeleteWorker(device, paths)
        worker = self._delete_worker
        worker.moveToThread(self._delete_thread)
        worker.delete_finished.connect(self._on_delete_finished)
        self._delete_thread.started.connect(worker.run)
        self._delete_thread.finished.connect(worker.deleteLater)
        self._delete_thread.start()

    @Slot(object, object)
    def _on_delete_finished(self, deleted, failed) -> None:
        self._deleting = False
        # The delete thread idles in its event loop once run() returns; stop
        # it here, or Qt aborts at shutdown ("thread destroyed while running").
        if getattr(self, "_delete_thread", None) is not None:
            self._delete_thread.quit()
            self._delete_thread.wait(3000)
            self._delete_thread = None
            self._delete_worker = None
        for library in self.libraries:
            library.remove_items(deleted)
        self._update_environment()
        log.info("Deleted %s item(s), %s failed", len(deleted), len(failed))
        if failed:
            names = "\n".join(failed[:8])
            if len(failed) > 8:
                names += "\n..."
            self._show_message(
                "Some items were not deleted",
                f"Deleted {len(deleted)}. Could not delete {len(failed)}:\n{names}\n\n"
                "The phone may protect some files. Unlock it and try again.",
            )
