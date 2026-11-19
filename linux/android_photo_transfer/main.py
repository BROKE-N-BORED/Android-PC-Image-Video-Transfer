"""Entry point wiring for the Android Photo Transfer Linux port.

Mirrors upstream App.xaml.cs: logging, settings, history (graceful
degradation: the app still works without history, only "Import New" loses
its memory), device manager, thumbnail cache, view models, main window,
global exception handler with a friendly dialog, shutdown save/dispose.
"""
from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from .core.errors import describe as describe_error
from .core.history import HistoryDatabase
from .core.logutil import setup_logging
from .core.settings import SettingsManager
from .core.transfer import TransferManager
from .device.manager import DeviceManager
from .ui.main_window import MainWindow
from .ui.thumbnails import DEFAULT_CACHE_MB, ThumbnailCache, ThumbnailManager
from .ui.viewmodels import MainViewModel

log = logging.getLogger("android_photo_transfer")

APP_NAME = "Android Photo Transfer"


def _install_exception_handler(app: QApplication) -> None:
    """Friendly dialog for unexpected errors; photos are never at risk."""

    def handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.error(
            "Unexpected error:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        try:
            QMessageBox.warning(
                None,
                APP_NAME,
                "Something unexpected went wrong, but your photos are safe.\n\n"
                + describe_error(exc_value)
                + "\n\nTechnical details were saved to the log (Settings: Open Logs).",
            )
        except Exception:
            pass

    sys.excepthook = handle


def main(argv=None) -> int:
    setup_logging()
    log.info("Application startup")

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Broke n Bored")
    _install_exception_handler(app)

    settings_manager = SettingsManager()
    settings = settings_manager.current

    history = None
    try:
        history = HistoryDatabase()
    except Exception as exc:
        # The app still works without history; only "Import New" loses its memory.
        log.error("Transfer history database is unavailable: %s", exc)

    cache = ThumbnailCache(settings.thumbnail_cache_mb * 1024 * 1024)
    thumbnails = ThumbnailManager(cache)
    thumbnails.enabled = settings.thumbnails_enabled

    transfer_engine = TransferManager(history)

    view_model_holder: dict = {}

    def on_devices_changed():
        vm = view_model_holder.get("vm")
        if vm is not None:
            vm.on_device_manager_changed()

    devices = DeviceManager(on_devices_changed)

    view_model = MainViewModel(
        settings_manager, devices, thumbnails, transfer_engine, history
    )
    view_model_holder["vm"] = view_model

    window = MainWindow(view_model, settings_manager, thumbnails)
    window.show()
    log.info("Main window shown")
    view_model.start()
    # One quiet update check per launch, after the window is up (only when
    # the setting is on; see MainWindow.start_silent_update_check).
    window.start_silent_update_check()

    try:
        return app.exec()
    finally:
        log.info("Application exit")
        try:
            view_model.shutdown()
        except Exception as exc:
            log.warning("Error during view model shutdown: %s", exc)
        try:
            devices.shutdown()
        except Exception as exc:
            log.warning("Error during device shutdown: %s", exc)
        try:
            thumbnails.shutdown()
        except Exception as exc:
            log.warning("Error during thumbnail shutdown: %s", exc)
        if history is not None:
            try:
                history.close()
            except Exception as exc:
                log.warning("Error closing history: %s", exc)
        try:
            settings_manager.save()
        except Exception as exc:
            log.warning("Error saving settings: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
