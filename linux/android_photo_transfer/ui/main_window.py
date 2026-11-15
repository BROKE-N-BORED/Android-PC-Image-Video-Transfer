"""Main window: header, two tabs, state panels, transfer bar, status bar.

Mirrors upstream MainWindow.xaml. The transfer panel and the message panel
are dialogs here (Qt has no WPF-style overlay grid); the delete flow uses
the safety fix from the contract, not the upstream single-confirm dialog.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..device.types import ConnectionState
from .files_view import FilesView
from .helpers import (
    ACCENT,
    CHROME,
    DIVIDER,
    ERROR,
    SUBTLE_TEXT,
    SUCCESS,
    WARNING,
    format_bytes,
)
from .photos_view import PhotosView
from .preview import PreviewWindow
from .settings_window import SettingsWindow

log = logging.getLogger("android_photo_transfer")

WINDOW_WIDTH = 1220
WINDOW_HEIGHT = 780
MIN_WIDTH = 960
MIN_HEIGHT = 580


class MainWindow(QMainWindow):
    def __init__(self, view_model, settings_manager, thumbnails, parent=None):
        super().__init__(parent)
        self._vm = view_model
        self._settings_manager = settings_manager
        self._thumbnails = thumbnails
        self._preview_window: PreviewWindow | None = None
        self._transfer_dialog: _TransferDialog | None = None

        self.setWindowTitle("Android Photo Transfer")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._restore_geometry()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        # Tabs + state panels share one stacked area (panels cover both tabs).
        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.photos_view = PhotosView(view_model.media_library, thumbnails)
        self.files_view = FilesView(view_model.files_library, thumbnails)
        self._tabs.addTab(self.photos_view, "Photos and Videos")
        self._tabs.addTab(self.files_view, "Files")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._stack.addWidget(self._tabs)
        self._waiting_panel = self._build_waiting_panel()
        self._stack.addWidget(self._waiting_panel)
        self._connecting_panel = self._build_connecting_panel()
        self._stack.addWidget(self._connecting_panel)
        self._locked_panel = self._build_locked_panel()
        self._stack.addWidget(self._locked_panel)
        self._error_panel = self._build_error_panel()
        self._stack.addWidget(self._error_panel)

        layout.addWidget(self._build_transfer_bar())

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Starting...")
        self._status_label.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 12px;")
        self._status_bar.addWidget(self._status_label, 1)
        self._scan_progress = QProgressBar()
        self._scan_progress.setMaximum(0)  # indeterminate
        self._scan_progress.setFixedSize(110, 8)
        self._scan_progress.setTextVisible(False)
        self._status_bar.addPermanentWidget(self._scan_progress)
        self._scan_progress.hide()

        # Shortcuts: Ctrl+A ticks everything in the current view; F5 rescans.
        select_all = QShortcut(QKeySequence("Ctrl+A"), self)
        select_all.activated.connect(lambda: self._vm.selected_library.select_all())
        refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        refresh_shortcut.activated.connect(self._vm.refresh)

        # Thumbnail visibility probe: only the on-screen Photos grid loads.
        view_model.set_thumbnail_visibility_probe(self._item_is_on_screen)

        # Wiring.
        view_model.device_changed.connect(self._sync_device)
        view_model.status_changed.connect(self._sync_status)
        view_model.panels_changed.connect(self._sync_panels)
        view_model.environment_changed.connect(self._sync_transfer_bar)
        view_model.message_requested.connect(self._show_message)
        view_model.preview_requested.connect(self._open_preview)
        view_model.settings_requested.connect(self._open_settings)
        view_model.delete_dialog_requested.connect(self._show_delete_dialog)
        for library in view_model.libraries:
            library.selection_changed.connect(self._sync_transfer_bar)
            library.state_changed.connect(self._sync_transfer_bar)
            library.counts_changed.connect(self._sync_tab_counts)
        view_model.transfer.progress_changed.connect(self._sync_transfer_dialog)
        view_model.transfer.state_changed.connect(self._sync_transfer_dialog)

        self._sync_device()
        self._sync_status()
        self._sync_panels()
        self._sync_transfer_bar()
        self._sync_tab_counts()

    # -- header -------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setStyleSheet(f"background: {CHROME}; border-bottom: 1px solid {DIVIDER};")
        header.setFixedHeight(44)
        row = QHBoxLayout(header)
        row.setContentsMargins(14, 0, 14, 0)
        row.setSpacing(8)

        self._device_label = QLabel("No phone connected")
        font = self._device_label.font()
        font.setBold(True)
        font.setPointSize(11)
        self._device_label.setFont(font)
        row.addWidget(self._device_label)

        self._phone_combo = QComboBox()
        self._phone_combo.setMinimumWidth(220)
        self._phone_combo.setToolTip("Choose which phone to use")
        self._phone_combo.currentIndexChanged.connect(self._on_phone_chosen)
        self._phone_combo.hide()
        row.addWidget(self._phone_combo)

        self._status_dot = QLabel()
        self._status_dot.setFixedSize(9, 9)
        row.addWidget(self._status_dot)
        self._connection_label = QLabel("Not connected")
        self._connection_label.setStyleSheet(f"color: {SUBTLE_TEXT};")
        row.addWidget(self._connection_label)

        row.addStretch(1)

        refresh = QPushButton("Refresh / Rescan Phone")
        refresh.setToolTip(
            "Reconnect to the phone and rescan it: use this after taking new photos "
            "or if anything looks stuck (F5)"
        )
        refresh.clicked.connect(self._vm.refresh)
        self._refresh_button = refresh
        row.addWidget(refresh)

        settings = QPushButton("Settings")
        settings.clicked.connect(self._vm.open_settings)
        row.addWidget(settings)
        return header

    def _on_phone_chosen(self, index: int) -> None:
        device_id = self._phone_combo.itemData(index)
        if device_id:
            self._vm.select_phone(device_id)

    # -- state panels ---------------------------------------------------------------
    @staticmethod
    def _state_panel(title: str, lines, button_text=None, on_button=None, progress=False):
        panel = QWidget()
        panel.setStyleSheet("background: white;")
        outer = QVBoxLayout(panel)
        outer.addStretch(1)
        inner = QVBoxLayout()
        inner.setSpacing(6)
        title_label = QLabel(title)
        title_font = title_label.font()
        title_font.setPointSize(20)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(title_label)
        labels = []
        for text in lines:
            label = QLabel(text)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 14px;")
            inner.addWidget(label)
            labels.append(label)
        if progress:
            bar = QProgressBar()
            bar.setMaximum(0)
            bar.setFixedSize(220, 6)
            bar.setTextVisible(False)
            bar_wrap = QHBoxLayout()
            bar_wrap.addStretch(1)
            bar_wrap.addWidget(bar)
            bar_wrap.addStretch(1)
            inner.addLayout(bar_wrap)
        if button_text is not None:
            button = QPushButton(button_text)
            button.setMinimumWidth(120)
            if on_button is not None:
                button.clicked.connect(on_button)
            button_wrap = QHBoxLayout()
            button_wrap.addStretch(1)
            button_wrap.addWidget(button)
            button_wrap.addStretch(1)
            inner.addSpacing(14)
            inner.addLayout(button_wrap)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addLayout(inner)
        wrap.addStretch(1)
        outer.addLayout(wrap)
        outer.addStretch(1)
        panel.setMaximumWidth(16777215)
        return panel, labels, title_label

    def _build_waiting_panel(self):
        panel, _, _ = self._state_panel(
            "PHONE NOT CONNECTED",
            [
                "Connect your Android phone to this PC with a USB cable.",
                "Waiting for device...",
                'If prompted on your phone, choose "File Transfer" or "MTP".',
                "Plugged in but nothing happens? Some cables only charge: try a different (data) cable.",
            ],
            button_text="Check Again",
            on_button=self._vm.check_again,
        )
        return panel

    def _build_connecting_panel(self):
        panel, _, title_label = self._state_panel(
            "Connecting...",
            [],
            progress=True,
        )
        self._connecting_title = title_label
        return panel

    def _build_locked_panel(self):
        panel, _, _ = self._state_panel(
            "PHONE DETECTED",
            [
                "This PC can see your phone, but its storage is unavailable.",
                "1.  Unlock your phone.",
                '2.  Select "File Transfer" if prompted (tap Allow if asked about access to phone data).',
                "(No prompt? Swipe down from the top of the phone's screen, tap the USB "
                "notification and choose File Transfer.)",
                "Waiting...",
            ],
        )
        return panel

    def _build_error_panel(self):
        panel, labels, _ = self._state_panel(
            "COULD NOT CONNECT TO THE PHONE",
            ["Connection problem", "Retrying automatically..."],
            button_text="Try Again",
            on_button=self._vm.refresh,
        )
        self._error_message_label = labels[0] if labels else None
        return panel

    # -- transfer bar ---------------------------------------------------------------
    def _build_transfer_bar(self) -> QWidget:
        bar = QFrame()
        bar.setStyleSheet(f"background: {CHROME}; border-top: 1px solid {DIVIDER};")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self._selection_label = QLabel()
        font = self._selection_label.font()
        font.setBold(True)
        self._selection_label.setFont(font)
        top.addWidget(self._selection_label)
        self._untick_button = QPushButton()
        self._untick_button.setFlat(True)
        self._untick_button.setStyleSheet(f"color: {ACCENT};")
        self._untick_button.setToolTip("Deselect the ticked items that were already copied to this PC")
        self._untick_button.clicked.connect(lambda: self._vm.selected_library.untick_transferred())
        top.addWidget(self._untick_button)
        top.addStretch(1)
        layout.addLayout(top)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Save To:"))
        self._dest_label = QLabel()
        self._dest_label.setStyleSheet("background: white; border: 1px solid #CFCFCF; padding: 4px 6px;")
        bottom.addWidget(self._dest_label, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._vm.choose_destination(self))
        self._browse_button = browse
        bottom.addWidget(browse)
        open_dest = QPushButton("Open")
        open_dest.setToolTip("Open the destination folder")
        open_dest.clicked.connect(self._vm.open_destination)
        bottom.addWidget(open_dest)

        bottom.addSpacing(8)
        self._select_all_button = QPushButton("Select All")
        self._select_all_button.setToolTip("Tick everything in the current view (Ctrl+A)")
        self._select_all_button.clicked.connect(lambda: self._vm.selected_library.select_all())
        bottom.addWidget(self._select_all_button)
        self._clear_button = QPushButton("Clear")
        self._clear_button.clicked.connect(lambda: self._vm.selected_library.clear_selection())
        bottom.addWidget(self._clear_button)
        self._delete_button = QPushButton("Delete from Phone")
        self._delete_button.setStyleSheet(
            f"QPushButton {{ color: {ERROR}; }} "
            f"QPushButton:disabled {{ color: #9AA0A6; }}"
        )
        self._delete_button.setToolTip("Permanently delete the ticked items from the phone (asks first)")
        self._delete_button.clicked.connect(lambda: self._vm.selected_library.delete_selected())
        bottom.addWidget(self._delete_button)
        self._import_new_button = QPushButton("Import New")
        self._import_new_button.setToolTip("Copy only what has not been transferred from this phone before")
        self._import_new_button.clicked.connect(lambda: self._vm.selected_library.import_new())
        bottom.addWidget(self._import_new_button)
        self._transfer_all_button = QPushButton("Transfer All")
        self._transfer_all_button.setToolTip("Copy everything in this tab (files already on this PC are skipped)")
        self._transfer_all_button.clicked.connect(lambda: self._vm.selected_library.transfer_all())
        bottom.addWidget(self._transfer_all_button)
        self._transfer_button = QPushButton("TRANSFER")
        self._transfer_button.setMinimumWidth(136)
        self._transfer_button.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: white; font-weight: bold; "
            f"border: none; border-radius: 4px; padding: 6px 12px; }} "
            f"QPushButton:disabled {{ background: #B0B0B0; }}"
        )
        self._transfer_button.clicked.connect(lambda: self._vm.selected_library.transfer_selected())
        bottom.addWidget(self._transfer_button)
        layout.addLayout(bottom)
        return bar

    # -- sync -------------------------------------------------------------------------
    def _on_tab_changed(self, index: int) -> None:
        self._vm.set_selected_tab(index)
        self._sync_transfer_bar()

    def _sync_device(self) -> None:
        vm = self._vm
        self._device_label.setText(vm.device_name)
        self._connection_label.setText(vm.connection_text)
        color = {
            ConnectionState.READY: SUCCESS,
            ConnectionState.CONNECTING: WARNING,
            ConnectionState.LOCKED: WARNING,
            ConnectionState.ERROR: ERROR,
            ConnectionState.WAITING: "#9E9E9E",
        }.get(vm.connection_state, "#9E9E9E")
        self._status_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        if vm.has_multiple_phones:
            self._device_label.hide()
            self._phone_combo.show()
            self._phone_combo.blockSignals(True)
            self._phone_combo.clear()
            for phone in vm.phones:
                self._phone_combo.addItem(phone.name, phone.device_id)
            if vm.selected_phone_id:
                idx = self._phone_combo.findData(vm.selected_phone_id)
                if idx >= 0:
                    self._phone_combo.setCurrentIndex(idx)
            self._phone_combo.blockSignals(False)
        else:
            self._device_label.show()
            self._phone_combo.hide()
        self._phone_combo.setEnabled(vm.is_idle)
        self._refresh_button.setEnabled(vm.is_idle)
        if self._connecting_title is not None:
            self._connecting_title.setText(f"Connecting to {vm.device_name}...")
        if self._error_message_label is not None:
            self._error_message_label.setText(vm.device_error_message or "Connection problem")
        self._scan_progress.setVisible(vm.is_scanning)

    def _sync_status(self) -> None:
        self._status_label.setText(self._vm.status_text)

    def _sync_panels(self) -> None:
        vm = self._vm
        if vm.show_waiting_panel:
            self._stack.setCurrentWidget(self._waiting_panel)
        elif vm.show_connecting_panel:
            self._stack.setCurrentWidget(self._connecting_panel)
        elif vm.show_locked_panel:
            self._stack.setCurrentWidget(self._locked_panel)
        elif vm.show_error_panel:
            self._stack.setCurrentWidget(self._error_panel)
        else:
            self._stack.setCurrentWidget(self._tabs)

    def _sync_tab_counts(self) -> None:
        media = self._vm.media_library.item_count
        files = self._vm.files_library.item_count
        self._tabs.setTabText(0, f"Photos and Videos  ({media})" if media else "Photos and Videos")
        self._tabs.setTabText(1, f"Files  ({files})" if files else "Files")

    def _sync_transfer_bar(self) -> None:
        lib = self._vm.selected_library
        self._selection_label.setText(lib.selection_text)
        self._untick_button.setText(lib.untick_transferred_text)
        self._untick_button.setVisible(lib.selected_transferred_count > 0)
        self._dest_label.setText(self._vm.destination_folder)
        self._dest_label.setToolTip(self._vm.destination_folder)
        self._browse_button.setEnabled(self._vm.is_idle)
        self._select_all_button.setEnabled(lib.can_select)
        self._clear_button.setEnabled(lib.can_select)
        self._delete_button.setEnabled(lib.can_delete)
        self._import_new_button.setEnabled(lib.can_import_new)
        self._import_new_button.setText(lib.import_new_text)
        self._transfer_all_button.setEnabled(lib.can_transfer_all)
        self._transfer_button.setEnabled(lib.can_transfer_selected)
        self._transfer_button.setText(lib.transfer_button_text)
        # The scan ends without a device change, so the progress bar must
        # follow the environment sync too, or it stays visible forever.
        self._scan_progress.setVisible(self._vm.is_scanning)

    # -- thumbnails ----------------------------------------------------------------------
    def _item_is_on_screen(self, item) -> bool:
        if self._tabs.currentIndex() != 0:
            return False
        return self.photos_view.item_is_on_screen(item)

    # -- dialogs ---------------------------------------------------------------------------
    def _show_message(self, payload: dict) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(payload.get("title", "Android Photo Transfer"))
        body = payload.get("body", "")
        warning = payload.get("warning")
        if warning:
            body += f"\n\n{warning}"
        box.setText(body)
        box.setIcon(QMessageBox.Icon.Information)
        if payload.get("offer_folder_change"):
            choose = box.addButton("Choose Another Folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() is choose:
                self._vm.choose_destination(self)
        else:
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()

    def _open_preview(self, items, index: int) -> None:
        device = self._vm.active_device
        if device is None:
            return
        if self._preview_window is not None:
            self._preview_window.close()
        self._preview_window = PreviewWindow(
            items, index, device, lambda vm: self._vm.start_transfer([vm]), self
        )
        self._preview_window.show()

    def _open_settings(self) -> None:
        dialog = SettingsWindow(
            self._settings_manager, self._thumbnails.cache,
            self._vm.apply_settings, self,
        )
        dialog.exec()

    def _show_delete_dialog(self, payload: dict) -> None:
        """Delete-from-phone safety flow (the contract's fix, not upstream's hole).

        By default only items already on the PC are deleted; untransferred
        items are excluded and named. Deleting untransferred items needs a
        checkbox plus a second, separate confirmation dialog.
        """
        items = payload["items"]
        on_pc = payload["on_pc"]
        not_on_pc = payload["not_on_pc"]
        device_name = payload["device_name"]
        total_bytes = payload["total_bytes"]

        dialog = QDialog(self)
        dialog.setWindowTitle("Delete from phone?")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 16)

        noun = "item" if len(items) == 1 else "items"
        on_pc_noun = "item" if len(on_pc) == 1 else "items"
        if not_on_pc:
            body = (
                f"Permanently delete {len(on_pc)} {on_pc_noun} ({format_bytes(sum(v.item.size for v in on_pc))}) "
                f"from {device_name}?\n\nThis cannot be undone."
                if on_pc else
                "None of the selected items have been copied to this PC yet, so nothing "
                "will be deleted unless you tick the box below."
            )
        else:
            body = (
                f"Permanently delete {len(items)} {noun} ({format_bytes(total_bytes)}) "
                f"from {device_name}?\n\nThis cannot be undone."
            )
        body_label = QLabel(body)
        body_label.setWordWrap(True)
        layout.addWidget(body_label)

        include_check = None
        if not_on_pc:
            names = [v.item.name for v in not_on_pc[:8]]
            extra = len(not_on_pc) - len(names)
            excluded = "\n".join(f"  •  {name}" for name in names)
            if extra > 0:
                excluded += f"\n  •  and {extra} more"
            not_on_pc_noun = "item" if len(not_on_pc) == 1 else "items"
            have = "has" if len(not_on_pc) == 1 else "have"
            pronoun = "it" if len(not_on_pc) == 1 else "they"
            warning = QLabel(
                f"{len(not_on_pc)} of these {have} NOT been copied to this PC yet, so {pronoun} "
                f"will be left on the phone:\n{excluded}"
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {ERROR}; font-weight: bold;")
            layout.addWidget(warning)
            include_check = QCheckBox(
                f"Also delete the {len(not_on_pc)} {not_on_pc_noun} not on this PC yet"
            )
            layout.addWidget(include_check)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        delete = QPushButton("Delete")
        delete.setStyleSheet(
            f"QPushButton {{ background: {ERROR}; color: white; font-weight: bold; }} "
            f"QPushButton:disabled {{ background: #B0B0B0; color: white; }}"
        )
        delete.setMinimumWidth(90)
        if include_check is not None and not on_pc:
            # Nothing would be deleted by default; require the checkbox.
            delete.setEnabled(False)
            include_check.toggled.connect(delete.setEnabled)
        delete.clicked.connect(dialog.accept)
        buttons.addWidget(delete)
        layout.addLayout(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        include_untransferred = bool(include_check is not None and include_check.isChecked())
        if include_untransferred:
            second = QMessageBox(self)
            second.setWindowTitle("Delete items not on this PC?")
            second.setIcon(QMessageBox.Icon.Warning)
            if len(not_on_pc) == 1:
                second_text = (
                    "This 1 item is NOT on this PC yet. "
                    "Deleting it will lose it forever.\n\nAre you sure?"
                )
            else:
                second_text = (
                    f"These {len(not_on_pc)} items are NOT on this PC yet. "
                    "Deleting them will lose them forever.\n\nAre you sure?"
                )
            second.setText(second_text)
            delete_anyway = second.addButton(
                "Delete them anyway", QMessageBox.ButtonRole.DestructiveRole
            )
            second.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            second.exec()
            if second.clickedButton() is not delete_anyway:
                return
        self._vm.confirm_delete(items, include_untransferred)

    # -- transfer dialog ----------------------------------------------------------------------
    def _sync_transfer_dialog(self) -> None:
        transfer = self._vm.transfer
        if transfer.is_visible:
            if self._transfer_dialog is None:
                self._transfer_dialog = _TransferDialog(transfer, self)
                self._transfer_dialog.show()
            self._transfer_dialog.refresh()
        elif self._transfer_dialog is not None:
            self._transfer_dialog.close()
            self._transfer_dialog = None

    # -- geometry -----------------------------------------------------------------------------
    def _restore_geometry(self) -> None:
        geometry = self._settings_manager.current.window_geometry
        if not geometry:
            self._center_on_screen()
            return
        try:
            x, y = int(geometry["x"]), int(geometry["y"])
            w, h = int(geometry["w"]), int(geometry["h"])
        except (KeyError, TypeError, ValueError):
            self._center_on_screen()
            return
        w = max(MIN_WIDTH, min(w, 4000))
        h = max(MIN_HEIGHT, min(h, 4000))
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QGuiApplication

        saved = QRect(x, y, w, h)
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(saved):
                self.setGeometry(x, y, w, h)
                return
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.x() + max(0, (available.width() - WINDOW_WIDTH) // 2)
        y = available.y() + max(0, (available.height() - WINDOW_HEIGHT) // 2)
        self.setGeometry(x, y, WINDOW_WIDTH, WINDOW_HEIGHT)

    def _save_geometry(self) -> None:
        rect = self.geometry()
        self._settings_manager.current.window_geometry = {
            "x": rect.x(), "y": rect.y(), "w": rect.width(), "h": rect.height(),
        }

    # -- close guard ----------------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._vm.transfer.is_running:
            answer = QMessageBox.question(
                self,
                "Transfer in progress",
                "A transfer is still running. Stop it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._vm.transfer.cancel()
        self._save_geometry()
        super().closeEvent(event)


class _TransferDialog(QDialog):
    """Transfer progress + completion panel (upstream transfer overlay)."""

    def __init__(self, transfer, parent=None):
        super().__init__(parent)
        self._transfer = transfer
        self.setWindowTitle("Transfer")
        self.setFixedWidth(560)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.CustomizeWindowHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)

        # Running page.
        self._running = QWidget()
        running = QVBoxLayout(self._running)
        running.setContentsMargins(0, 0, 0, 0)
        self._title_label = QLabel("Transferring")
        title_font = self._title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        self._title_label.setWordWrap(True)
        running.addWidget(self._title_label)
        self._current_label = QLabel()
        self._current_label.setWordWrap(True)
        running.addWidget(self._current_label)

        overall_row = QHBoxLayout()
        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 100)
        self._overall_bar.setTextVisible(False)
        self._overall_bar.setFixedHeight(10)
        overall_row.addWidget(self._overall_bar, 1)
        self._percent_label = QLabel("0%")
        percent_font = self._percent_label.font()
        percent_font.setBold(True)
        self._percent_label.setFont(percent_font)
        overall_row.addWidget(self._percent_label)
        running.addLayout(overall_row)

        self._file_bar = QProgressBar()
        self._file_bar.setRange(0, 100)
        self._file_bar.setTextVisible(False)
        self._file_bar.setFixedHeight(3)
        self._file_bar.setToolTip("Current file")
        running.addWidget(self._file_bar)

        counts_row = QHBoxLayout()
        self._files_label = QLabel()
        counts_row.addWidget(self._files_label)
        counts_row.addStretch(1)
        self._copied_label = QLabel()
        self._copied_label.setStyleSheet("background: #E6F4EA; color: #1E7B34; font-weight: bold; padding: 3px 10px; border-radius: 10px;")
        counts_row.addWidget(self._copied_label)
        self._skipped_label = QLabel()
        self._skipped_label.setStyleSheet("background: #EEF0F3; color: #4A5058; font-weight: bold; padding: 3px 10px; border-radius: 10px;")
        counts_row.addWidget(self._skipped_label)
        self._failed_label = QLabel()
        self._failed_label.setStyleSheet("background: #EEF0F3; color: #C42B1C; font-weight: bold; padding: 3px 10px; border-radius: 10px;")
        counts_row.addWidget(self._failed_label)
        running.addLayout(counts_row)
        self._bytes_label = QLabel()
        running.addWidget(self._bytes_label)

        stats_row = QHBoxLayout()
        self._speed_label = QLabel()
        self._elapsed_label = QLabel()
        self._remaining_label = QLabel()
        for label in (self._speed_label, self._elapsed_label, self._remaining_label):
            label.setStyleSheet(f"color: {SUBTLE_TEXT};")
            stats_row.addWidget(label)
        running.addLayout(stats_row)

        run_buttons = QHBoxLayout()
        run_buttons.addStretch(1)
        self._pause_button = QPushButton("Pause")
        self._pause_button.setMinimumWidth(96)
        self._pause_button.clicked.connect(transfer.toggle_pause)
        run_buttons.addWidget(self._pause_button)
        cancel_button = QPushButton("Cancel")
        cancel_button.setMinimumWidth(96)
        cancel_button.clicked.connect(transfer.cancel)
        run_buttons.addWidget(cancel_button)
        running.addLayout(run_buttons)
        layout.addWidget(self._running)

        # Complete page.
        self._complete = QWidget()
        complete = QVBoxLayout(self._complete)
        complete.setContentsMargins(0, 0, 0, 0)
        self._completion_title = QLabel()
        title_font2 = self._completion_title.font()
        title_font2.setPointSize(16)
        title_font2.setBold(True)
        self._completion_title.setFont(title_font2)
        self._completion_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        complete.addWidget(self._completion_title)
        self._completion_summary = QLabel()
        self._completion_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        complete.addWidget(self._completion_summary)
        self._completion_counts = QLabel()
        self._completion_counts.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._completion_counts.setStyleSheet(f"color: {SUBTLE_TEXT};")
        complete.addWidget(self._completion_counts)
        self._completion_detail = QLabel()
        self._completion_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._completion_detail.setWordWrap(True)
        self._completion_detail.setStyleSheet(f"color: {SUBTLE_TEXT};")
        complete.addWidget(self._completion_detail)
        self._failure_list = QListWidget()
        self._failure_list.setMaximumHeight(160)
        complete.addWidget(self._failure_list)
        done_row = QHBoxLayout()
        done_row.addStretch(1)
        self._details_button = QPushButton("View Details")
        self._details_button.clicked.connect(transfer.toggle_details)
        done_row.addWidget(self._details_button)
        self._retry_button = QPushButton("Retry Failed")
        self._retry_button.clicked.connect(transfer.retry_failed)
        done_row.addWidget(self._retry_button)
        self._resume_button = QPushButton("Resume")
        self._resume_button.clicked.connect(transfer.resume)
        done_row.addWidget(self._resume_button)
        open_button = QPushButton("Open Folder")
        open_button.clicked.connect(transfer.open_folder)
        done_row.addWidget(open_button)
        done_button = QPushButton("Done")
        done_button.setMinimumWidth(96)
        done_button.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: white; font-weight: bold; border: none; border-radius: 4px; padding: 6px 12px; }}"
        )
        done_button.clicked.connect(transfer.done)
        done_row.addWidget(done_button)
        done_row.addStretch(1)
        complete.addLayout(done_row)
        layout.addWidget(self._complete)

        self.refresh()

    def refresh(self) -> None:
        t = self._transfer
        self._running.setVisible(t.is_running)
        self._complete.setVisible(t.is_complete)
        if t.is_running:
            self._title_label.setText(t.title)
            self._current_label.setText(t.current_file)
            self._overall_bar.setValue(int(t.overall_percent))
            self._percent_label.setText(t.percent_text)
            self._file_bar.setValue(int(t.file_percent))
            self._files_label.setText(t.files_text)
            self._bytes_label.setText(t.bytes_text)
            self._copied_label.setText(f"Copied {t.live_copied}")
            self._skipped_label.setText(f"Skipped {t.live_skipped}")
            self._failed_label.setText(f"Failed {t.live_failed}")
            self._speed_label.setText(f"Current: {t.speed_text}")
            self._elapsed_label.setText(f"Elapsed: {t.elapsed_text}")
            self._remaining_label.setText(f"Remaining: {t.remaining_text}")
            self._pause_button.setText(t.pause_button_text)
        if t.is_complete:
            self._completion_title.setText(t.completion_title)
            self._completion_summary.setText(t.completion_summary)
            self._completion_counts.setText(t.completion_counts)
            self._completion_detail.setText(t.completion_detail)
            self._completion_detail.setVisible(bool(t.completion_detail))
            self._details_button.setVisible(t.has_failures)
            self._details_button.setText("Hide Details" if t.show_details else "View Details")
            self._failure_list.setVisible(t.show_details)
            if t.show_details:
                self._failure_list.clear()
                self._failure_list.addItems(t.failure_details)
            self._retry_button.setVisible(t.can_retry_failed)
            self._retry_button.setEnabled(t.resume_available)
            self._resume_button.setVisible(t.can_resume)
            self._resume_button.setEnabled(t.resume_available)
