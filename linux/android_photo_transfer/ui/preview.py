"""Preview window: double-click preview of one photo.

Mirrors upstream PreviewViewModel.cs: 150 MB size cap, 2400 px preview
width, prev/next through the visible list, transfer-from-preview. Video
preview is honestly unavailable (no video playback in this build).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..core.errors import describe as describe_error
from .helpers import SUBTLE_TEXT, format_bytes, format_date

log = logging.getLogger("android_photo_transfer")

MAX_PREVIEW_BYTES = 150 * 1024 * 1024  # upstream PreviewViewModel.MaxPreviewBytes
PREVIEW_WIDTH = 2400  # upstream PreviewViewModel.PreviewWidth


class _PreviewLoader(QObject):
    loaded = Signal(object, int, int)  # QImage, width, height
    failed = Signal(str)  # message

    def __init__(self, device, item):
        super().__init__()
        self._device = device
        self._item = item

    @Slot()
    def run(self) -> None:
        try:
            data = self._device.read_all_bytes(
                self._item.source_path, self._item.persistent_id, MAX_PREVIEW_BYTES
            )
        except Exception as exc:
            log.warning("Preview failed for %s: %s", self._item.name, exc)
            self.failed.emit(describe_error(exc))
            return
        image = QImage.fromData(data)
        if image.isNull():
            self.failed.emit(
                "This app cannot display this image. Transfer it to open it on this PC."
            )
            return
        width, height = image.width(), image.height()
        if width > PREVIEW_WIDTH:
            image = image.scaledToWidth(
                PREVIEW_WIDTH, Qt.TransformationMode.SmoothTransformation
            )
        self.loaded.emit(image, width, height)


class PreviewWindow(QDialog):
    """Double-click preview. Only the photo being previewed loads at high res."""

    def __init__(self, items, index: int, device, transfer_callback, parent=None):
        super().__init__(parent)
        self._items = list(items)
        self._index = max(0, min(index, len(self._items) - 1)) if self._items else 0
        self._device = device
        self._transfer_callback = transfer_callback
        self._loader_thread: QThread | None = None
        self._loader = None

        self.setWindowTitle("Preview")
        self.setMinimumSize(640, 480)
        self.resize(900, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        header = QHBoxLayout()
        self.title_label = QLabel()
        title_font = self.title_label.font()
        title_font.setBold(True)
        title_font.setPointSize(13)
        self.title_label.setFont(title_font)
        header.addWidget(self.title_label, 1)
        self.position_label = QLabel()
        self.position_label.setStyleSheet(f"color: {SUBTLE_TEXT};")
        header.addWidget(self.position_label)
        layout.addLayout(header)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background: #111111; color: white;")
        self.image_label.setMinimumSize(400, 300)
        layout.addWidget(self.image_label, 1)

        self.message_label = QLabel()
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 14px;")
        self.message_label.hide()
        layout.addWidget(self.message_label)

        self.details_label = QLabel()
        self.details_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_label.setStyleSheet(f"color: {SUBTLE_TEXT};")
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label)

        buttons = QHBoxLayout()
        self.prev_button = QPushButton("Previous")
        self.prev_button.clicked.connect(self._previous)
        buttons.addWidget(self.prev_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next)
        buttons.addWidget(self.next_button)
        buttons.addStretch(1)
        self.transfer_button = QPushButton("Transfer this photo")
        self.transfer_button.setDefault(True)
        self.transfer_button.clicked.connect(self._transfer)
        buttons.addWidget(self.transfer_button)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._load_current()

    # -- loading ------------------------------------------------------------------
    @property
    def _current(self):
        return self._items[self._index] if self._items else None

    def _load_current(self) -> None:
        self._stop_loader()
        vm = self._current
        if vm is None:
            return
        item = vm.item
        self.setWindowTitle(f"Preview: {item.name}")
        self.title_label.setText(item.name)
        self.position_label.setText(f"{self._index + 1} of {len(self._items)}")
        self.details_label.setText(self._build_details(item, None, None))
        self.image_label.clear()
        self.image_label.setText("Loading...")
        self.message_label.hide()
        self.prev_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index < len(self._items) - 1)

        if item.kind == "video":
            self.image_label.setText("")
            self.message_label.setText(
                "Video preview is not available. Transfer the video to play it on this PC."
            )
            self.message_label.show()
            return
        if item.kind != "photo":
            self.image_label.setText("")
            self.message_label.setText(
                "Preview is not available for this file. Transfer it to open it on this PC."
            )
            self.message_label.show()
            return
        if item.size > MAX_PREVIEW_BYTES:
            self.image_label.setText("")
            self.message_label.setText("This file is too large to preview. Transfer it to open it.")
            self.message_label.show()
            return

        self._loader_thread = QThread(self)
        self._loader_thread.setObjectName("preview")
        # Keep a reference: a worker held only by a signal connection can be
        # garbage-collected before its thread starts, and then run() never fires.
        self._loader = _PreviewLoader(self._device, item)
        loader = self._loader
        loader.moveToThread(self._loader_thread)
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._loader_thread.started.connect(loader.run)
        self._loader_thread.finished.connect(loader.deleteLater)
        self._loader_thread.start()

    @Slot(object, int, int)
    def _on_loaded(self, image: QImage, width: int, height: int) -> None:
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._on_failed("This app cannot display this image. Transfer it to open it on this PC.")
            return
        self.image_label.setText("")
        self._preview_pixmap = pixmap
        self._show_scaled_pixmap()
        vm = self._current
        if vm is not None:
            self.details_label.setText(self._build_details(vm.item, width, height))

    def _show_scaled_pixmap(self) -> None:
        pixmap = getattr(self, "_preview_pixmap", None)
        if pixmap is None:
            return
        scaled = pixmap.scaled(
            self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._show_scaled_pixmap()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.image_label.setText("")
        self.message_label.setText(message)
        self.message_label.show()

    @staticmethod
    def _build_details(item, width, height) -> str:
        parts = []
        if width and height:
            parts.append(f"{width} x {height}")
        parts.append(format_bytes(item.size))
        date = item.modified or item.created
        if date is not None:
            parts.append(format_date(date))
        parts.append(item.folder or "Phone storage")
        return "     •     ".join(parts)

    # -- navigation ------------------------------------------------------------------
    def _previous(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._load_current()

    def _next(self) -> None:
        if self._index < len(self._items) - 1:
            self._index += 1
            self._load_current()

    def _transfer(self) -> None:
        vm = self._current
        if vm is not None:
            self._transfer_callback(vm)
        self.close()

    def _stop_loader(self) -> None:
        if self._loader_thread is not None:
            self._loader_thread.quit()
            self._loader_thread.wait(2000)
            self._loader_thread = None
        self._loader = None

    def closeEvent(self, event) -> None:
        self._stop_loader()
        super().closeEvent(event)
