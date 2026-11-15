"""Settings window, mirroring upstream SettingsWindow.xaml.

Destination folder, skip-existing, open-folder-after-transfer,
organize-by-phone-folder, thumbnails on/off, cache size, Open Logs button.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .helpers import SUBTLE_TEXT, format_bytes, logs_dir, open_folder, pick_folder

CACHE_SIZES_MB = [32, 64, 128, 256, 512]


class SettingsWindow(QDialog):
    def __init__(self, settings_manager, thumbnail_cache, on_saved, parent=None):
        super().__init__(parent)
        self._manager = settings_manager
        self._cache = thumbnail_cache
        self._on_saved = on_saved
        settings = settings_manager.current

        self.setWindowTitle("Settings")
        self.setFixedWidth(520)
        self.setWindowModality(Qt.WindowModality.WindowModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 20)
        layout.setSpacing(0)

        layout.addWidget(self._header("GENERAL"))
        layout.addWidget(QLabel("Default destination"))
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit(settings.dest_folder)
        self.dest_edit.setReadOnly(True)
        dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        dest_row.addWidget(browse)
        layout.addLayout(dest_row)

        self.open_folder_check = QCheckBox("Open the destination folder after a transfer")
        self.open_folder_check.setChecked(settings.open_folder_after_transfer)
        layout.addWidget(self.open_folder_check)
        self.organize_check = QCheckBox(
            "Put photos in subfolders matching the phone (Camera, Screenshots...)"
        )
        self.organize_check.setChecked(settings.organize_by_folder)
        layout.addWidget(self.organize_check)

        layout.addWidget(self._header("TRANSFER"))
        existing_row = QGridLayout()
        existing_row.addWidget(
            QLabel("When the same file is already on this PC:"), 0, 0
        )
        self.existing_combo = QComboBox()
        self.existing_combo.addItem("Skip", True)
        self.existing_combo.addItem("Keep both (add _1, _2...)", False)
        self.existing_combo.setCurrentIndex(0 if settings.skip_existing else 1)
        existing_row.addWidget(self.existing_combo, 0, 1)
        layout.addLayout(existing_row)
        note = QLabel(
            "Files are never overwritten. A different photo with the same name "
            "is saved as name_1, name_2..."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 12px;")
        layout.addWidget(note)

        layout.addWidget(self._header("PERFORMANCE"))
        self.thumbnails_check = QCheckBox("Show thumbnails while browsing")
        self.thumbnails_check.setChecked(settings.thumbnails_enabled)
        layout.addWidget(self.thumbnails_check)
        cache_row = QGridLayout()
        cache_row.addWidget(QLabel("Thumbnail memory limit (MB)"), 0, 0)
        self.cache_combo = QComboBox()
        for size in CACHE_SIZES_MB:
            self.cache_combo.addItem(str(size), size)
        current_mb = settings.thumbnail_cache_mb
        if current_mb in CACHE_SIZES_MB:
            self.cache_combo.setCurrentIndex(CACHE_SIZES_MB.index(current_mb))
        else:
            self.cache_combo.addItem(str(current_mb), current_mb)
            self.cache_combo.setCurrentIndex(self.cache_combo.count() - 1)
        cache_row.addWidget(self.cache_combo, 0, 1)
        layout.addLayout(cache_row)

        layout.addWidget(self._header("APPLICATION"))
        app_row = QHBoxLayout()
        clear_cache = QPushButton("Clear Thumbnail Cache")
        clear_cache.clicked.connect(self._clear_cache)
        app_row.addWidget(clear_cache)
        open_logs = QPushButton("Open Logs")
        open_logs.clicked.connect(lambda: open_folder(str(logs_dir())))
        app_row.addWidget(open_logs)
        app_row.addStretch(1)
        layout.addLayout(app_row)
        self.cache_usage_label = QLabel()
        self.cache_usage_label.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 12px;")
        layout.addWidget(self.cache_usage_label)
        self._update_cache_usage()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("Save")
        save.setDefault(True)
        save.setMinimumWidth(90)
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        cancel = QPushButton("Cancel")
        cancel.setMinimumWidth(90)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        layout.setSpacing(6)

    @staticmethod
    def _header(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {SUBTLE_TEXT}; font-size: 11px; font-weight: bold; padding: 18px 0 8px 0;"
        )
        return label

    def _update_cache_usage(self) -> None:
        self.cache_usage_label.setText(
            f"{len(self._cache)} thumbnails in memory ({format_bytes(self._cache.current_bytes)})"
        )

    def _browse(self) -> None:
        folder = pick_folder(self, self.dest_edit.text(), "Choose the default destination folder")
        if folder:
            self.dest_edit.setText(folder)

    def _clear_cache(self) -> None:
        self._cache.clear()
        self._update_cache_usage()

    def _save(self) -> None:
        settings = self._manager.current
        settings.dest_folder = self.dest_edit.text()
        settings.skip_existing = bool(self.existing_combo.currentData())
        settings.open_folder_after_transfer = self.open_folder_check.isChecked()
        settings.organize_by_folder = self.organize_check.isChecked()
        settings.thumbnails_enabled = self.thumbnails_check.isChecked()
        settings.thumbnail_cache_mb = int(self.cache_combo.currentData())
        self._manager.save()
        self._on_saved()
        self.accept()
