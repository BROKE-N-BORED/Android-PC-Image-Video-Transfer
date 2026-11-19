"""Update UI: the update dialog, the download progress dialog, and the
worker objects that drive the Qt-free updater core (core/updates.py).

Workers follow the codebase's established shape (viewmodels.py): a QObject
worker moved onto a QThread, results back over signals, never blocking the
GUI thread. The core module is imported as a module (``updates``) so tests
and the E2E harness can monkeypatch ``updates.check_for_update`` /
``updates.download_update`` / ``updates.apply_update`` / ``updates.is_installed``
and the UI follows.
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .. import __version__
from ..core import updates
from .helpers import ACCENT, SUBTLE_TEXT, format_bytes

log = logging.getLogger("android_photo_transfer")

PHASE_DOWNLOADING = "Downloading the update..."
PHASE_INSTALLING = "Installing the update..."


class UpdateCheckWorker(QObject):
    """Runs check_for_update off the GUI thread. Never raises: the core
    returns {"state": "unknown", "reason": ...} for network trouble, and
    anything unexpected is caught here and reported the same way."""

    finished = Signal(object)  # check_for_update result dict

    @Slot()
    def run(self) -> None:
        try:
            result = updates.check_for_update(__version__)
        except Exception as exc:  # noqa: BLE001 - a failed check is a result, not a crash
            from ..core.errors import describe

            log.error("Update check failed unexpectedly: %s", exc)
            result = {"state": "unknown", "latest": None, "reason": describe(exc)}
        self.finished.emit(result)


class UpdateApplyWorker(QObject):
    """Downloads (with SHA check) then applies an update, off the GUI
    thread. The cancel event is checked between download chunks by the
    core; the apply step is local file work and runs to completion."""

    progress_changed = Signal(object, object)  # (downloaded bytes, total bytes or None)
    phase_changed = Signal(str)  # plain words: which step is running
    finished = Signal(str)  # new version
    failed = Signal(object)  # Exception (UpdateError is already plain language)
    cancelled = Signal()

    def __init__(self, info, dest_dir, cancel_event: threading.Event):
        super().__init__()
        self._info = info
        self._dest_dir = dest_dir
        self._cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            self.phase_changed.emit(PHASE_DOWNLOADING)
            downloaded = updates.download_update(
                self._info,
                self._dest_dir,
                progress_cb=lambda done, total: self.progress_changed.emit(done, total),
                cancel=self._cancel_event.is_set,
            )
            self.phase_changed.emit(PHASE_INSTALLING)
            updates.apply_update(downloaded["zip_path"], downloaded["version"])
        except updates.UpdateCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:  # noqa: BLE001 - any failure is reported, not raised
            self.failed.emit(exc)
            return
        self.finished.emit(downloaded["version"])


class UpdateDialog(QDialog):
    """Modal update offer.

    Installed: Update / Later. ``update_requested`` is True only when the
    user tapped Update, so the caller can tell that apart from the source
    run's "Open the release page" (which opens the page and closes).
    Source run: Open the release page / Close. This dialog never downloads
    or applies anything itself.
    """

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self._info = info
        self.update_requested = False
        latest = info.get("version") or info.get("latest") or ""

        self.setWindowTitle("Update available")
        self.setMinimumWidth(460)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)

        body = QLabel(
            f"Android Photo Transfer {latest} is on GitHub. "
            f"You have {__version__}."
        )
        body.setWordWrap(True)
        layout.addWidget(body)

        installed = updates.is_installed()
        if installed:
            note = QLabel("Your photos, settings and history stay where they are.")
        else:
            note = QLabel(
                "This copy is running from source, so it cannot update itself. "
                "Open the release page to get the new version."
            )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {SUBTLE_TEXT}; font-size: 12px;")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if installed:
            later = QPushButton("Later")
            later.setMinimumWidth(90)
            later.clicked.connect(self.reject)
            buttons.addWidget(later)
            update = QPushButton("Update")
            update.setMinimumWidth(90)
            update.setDefault(True)
            update.setStyleSheet(
                f"QPushButton {{ background: {ACCENT}; color: white; font-weight: bold; "
                f"border: none; border-radius: 4px; padding: 6px 12px; }} "
                f"QPushButton:disabled {{ background: #B0B0B0; }}"
            )
            update.clicked.connect(self._on_update)
            buttons.addWidget(update)
        else:
            close = QPushButton("Close")
            close.setMinimumWidth(90)
            close.clicked.connect(self.reject)
            buttons.addWidget(close)
            open_page = QPushButton("Open the release page")
            open_page.setMinimumWidth(90)
            open_page.setDefault(True)
            open_page.setEnabled(bool(info.get("page_url")))
            open_page.clicked.connect(self._open_release_page)
            buttons.addWidget(open_page)
        layout.addLayout(buttons)

    def _on_update(self) -> None:
        self.update_requested = True
        self.accept()

    def _open_release_page(self) -> None:
        url = self._info.get("page_url") or ""
        if url:
            opened = QDesktopServices.openUrl(QUrl(url))
            log.info("Opened the release page: %s", "ok" if opened else "failed")
        self.accept()


class UpdateProgressDialog(QDialog):
    """Download + install progress. Indeterminate bar (the download size
    is not always known); the bytes label shows real numbers. Cancel sets
    the shared event the worker's download loop checks between chunks."""

    def __init__(self, cancel_event: threading.Event, parent=None):
        super().__init__(parent)
        self._cancel_event = cancel_event
        self.setWindowTitle("Updating")
        self.setFixedWidth(480)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.CustomizeWindowHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel("Updating")
        title_font = title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        self._status_label = QLabel(PHASE_DOWNLOADING)
        layout.addWidget(self._status_label)

        self._bar = QProgressBar()
        self._bar.setMaximum(0)  # indeterminate
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(10)
        layout.addWidget(self._bar)

        self._bytes_label = QLabel("Downloaded 0 B")
        layout.addWidget(self._bytes_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setMinimumWidth(96)
        self._cancel_button.clicked.connect(self._on_cancel)
        buttons.addWidget(self._cancel_button)
        layout.addLayout(buttons)

    def set_progress(self, downloaded: int, total) -> None:
        if total:
            self._bytes_label.setText(
                f"Downloaded {format_bytes(downloaded)} of {format_bytes(total)}"
            )
        else:
            self._bytes_label.setText(f"Downloaded {format_bytes(downloaded)}")

    def set_phase(self, phase: str) -> None:
        self._status_label.setText(phase)
        if phase == PHASE_INSTALLING:
            # The download is done and the install is local file work:
            # there is nothing left to cancel.
            self._cancel_button.setEnabled(False)

    def _on_cancel(self) -> None:
        self._cancel_event.set()
        self._cancel_button.setEnabled(False)
        self._cancel_button.setText("Cancelling...")
