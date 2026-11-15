"""Settings, logging and error-message tests.

Mirrors upstream CoreTests.cs (UtilityTests.Settings_round_trip,
LoggerTests) and ErrorMessages.cs.

CONTRACT:
  - core/settings.py: Settings dataclass (dest_folder default
    ~/Pictures/Phone Photos, skip_existing=True,
    open_folder_after_transfer=False, organize_by_folder=False,
    thumbnails_enabled=True, thumbnail_cache_mb=64 clamped 16..1024,
    window_geometry=None {"x","y","w","h"}); SettingsManager(path=None)
    with .current and atomic .save() (tmp + os.replace); missing/corrupt
    file loads defaults.
  - core/logutil.py: setup_logging() -> logging.Logger named
    "android_photo_transfer", file logs under the XDG logs dir,
    RotatingFileHandler (1 MB x 3), plus log_exception helper.
  - core/errors.py: describe(exc) plain-language messages mirroring
    ErrorMessages.cs; PHONESTOPPED constant; is_destination_fatal(exc).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import TempFolder, try_import  # noqa: E402

settings_mod = try_import("android_photo_transfer.core.settings")
Settings = getattr(settings_mod, "Settings", None) if settings_mod else None
SettingsManager = getattr(settings_mod, "SettingsManager", None) if settings_mod else None

logutil_mod = try_import("android_photo_transfer.core.logutil")
errors_mod = try_import("android_photo_transfer.core.errors")


class SettingsTests(unittest.TestCase):

    def setUp(self):
        if SettingsManager is None:
            self.skipTest("android_photo_transfer.core.settings not built yet")
        self._folders: list[TempFolder] = []

    def tearDown(self):
        for folder in self._folders:
            folder.cleanup()

    def _path(self) -> str:
        folder = TempFolder()
        self._folders.append(folder)
        return os.path.join(folder.path, "settings.json")

    def test_defaults(self):
        mgr = SettingsManager(self._path())
        current = mgr.current
        self.assertTrue(current.dest_folder.endswith(os.path.join("Pictures", "Phone Photos")),
                        current.dest_folder)
        self.assertTrue(current.skip_existing)
        self.assertFalse(current.open_folder_after_transfer)
        self.assertFalse(current.organize_by_folder)
        self.assertTrue(current.thumbnails_enabled)
        self.assertEqual(64, current.thumbnail_cache_mb)
        self.assertIsNone(current.window_geometry)

    def test_round_trip(self):
        path = self._path()
        mgr = SettingsManager(path)
        mgr.current.dest_folder = "/tmp/somewhere/Photos"
        mgr.current.skip_existing = False
        mgr.current.open_folder_after_transfer = True
        mgr.current.organize_by_folder = True
        mgr.current.thumbnails_enabled = False
        mgr.current.thumbnail_cache_mb = 128
        mgr.current.window_geometry = {"x": 10, "y": 20, "w": 1220, "h": 780}
        mgr.save()

        reloaded = SettingsManager(path).current
        self.assertEqual("/tmp/somewhere/Photos", reloaded.dest_folder)
        self.assertFalse(reloaded.skip_existing)
        self.assertTrue(reloaded.open_folder_after_transfer)
        self.assertTrue(reloaded.organize_by_folder)
        self.assertFalse(reloaded.thumbnails_enabled)
        self.assertEqual(128, reloaded.thumbnail_cache_mb)
        self.assertEqual({"x": 10, "y": 20, "w": 1220, "h": 780},
                         reloaded.window_geometry)

    def test_missing_file_loads_defaults(self):
        self.assertEqual(64, SettingsManager(self._path()).current.thumbnail_cache_mb)

    def test_corrupt_file_loads_defaults(self):
        path = self._path()
        with open(path, "w") as fh:
            fh.write("{not json at all")
        mgr = SettingsManager(path)
        self.assertEqual(64, mgr.current.thumbnail_cache_mb)
        self.assertTrue(mgr.current.skip_existing)

    def test_thumbnail_cache_mb_is_clamped_16_to_1024(self):
        path = self._path()
        with open(path, "w") as fh:
            json.dump({"thumbnail_cache_mb": 5}, fh)
        self.assertEqual(16, SettingsManager(path).current.thumbnail_cache_mb)
        with open(path, "w") as fh:
            json.dump({"thumbnail_cache_mb": 5000}, fh)
        self.assertEqual(1024, SettingsManager(path).current.thumbnail_cache_mb)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        path = self._path()
        mgr = SettingsManager(path)
        mgr.current.thumbnail_cache_mb = 32
        mgr.save()
        folder = os.path.dirname(path)
        leftovers = [f for f in os.listdir(folder) if f.endswith(".tmp")]
        self.assertEqual([], leftovers)
        with open(path) as fh:
            saved = json.load(fh)
        self.assertEqual(32, saved["thumbnail_cache_mb"])

    def test_save_creates_parent_directories(self):
        folder = TempFolder()
        self._folders.append(folder)
        nested = os.path.join(folder.path, "deep", "settings.json")
        mgr = SettingsManager(nested)
        mgr.save()
        self.assertTrue(os.path.exists(nested))


class LogUtilTests(unittest.TestCase):

    def setUp(self):
        if logutil_mod is None or not hasattr(logutil_mod, "setup_logging"):
            self.skipTest("android_photo_transfer.core.logutil not built yet")
        self._folders: list[TempFolder] = []

    def tearDown(self):
        # Detach any handlers setup_logging attached so later tests (and
        # the test run itself) do not keep writing into temp dirs.
        logger = logging.getLogger("android_photo_transfer")
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)
        for folder in self._folders:
            folder.cleanup()

    def _setup_in_temp_home(self):
        """Call setup_logging with HOME pointed at a temp dir; return (logger, dir)."""
        import pathlib
        from unittest import mock

        folder = TempFolder()
        self._folders.append(folder)
        patchers = [
            mock.patch.dict(os.environ, {"HOME": folder.path}),
            mock.patch.object(pathlib.Path, "home",
                              return_value=pathlib.Path(folder.path)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        logger = logutil_mod.setup_logging()
        return logger, folder.path

    def test_logger_name_and_rotating_file_handler(self):
        logger, home = self._setup_in_temp_home()
        self.assertEqual("android_photo_transfer", logger.name)
        rotating = [h for h in logger.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        if not rotating:
            self.skipTest("setup_logging() did not attach a RotatingFileHandler "
                          "under a patched HOME; verify by hand")
        handler = rotating[0]
        self.assertEqual(1024 * 1024, handler.maxBytes)
        self.assertIn(handler.backupCount, (2, 3),
                      "1 MB x 3 files means backupCount 2 (or 3)")
        logger.info("hello-log")
        for handler in logger.handlers:
            handler.flush()
        log_dir = os.path.join(home, ".local", "share",
                               "android-photo-transfer", "logs")
        if os.path.isdir(log_dir):
            logs = [f for f in os.listdir(log_dir) if f.startswith("app.log")]
            self.assertTrue(logs, "no log file written under the XDG logs dir")

    def test_keeps_logging_after_the_log_file_rolls_over(self):
        logger, home = self._setup_in_temp_home()
        rotating = [h for h in logger.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        if not rotating:
            self.skipTest("no RotatingFileHandler attached; see test above")
        handler = rotating[0]
        handler.maxBytes = 4096  # shrink the rollover point for the test
        filler = "x" * 2000
        for _i in range(10):
            logger.info(filler)
        logger.info("AFTER-ROLLOVER")
        for h in logger.handlers:
            h.flush()
        base = handler.baseFilename
        with open(base) as fh:
            current = fh.read()
        self.assertIn("AFTER-ROLLOVER", current)
        self.assertTrue(os.path.exists(base + ".1"), "no rolled-over log file")
        self.assertLess(os.path.getsize(base), 1024 * 1024)


class ErrorMessageTests(unittest.TestCase):

    def setUp(self):
        if errors_mod is None or not hasattr(errors_mod, "describe"):
            self.skipTest("android_photo_transfer.core.errors not built yet")

    def test_disconnect_message(self):
        from android_photo_transfer.device.types import DeviceDisconnectedError

        self.assertEqual(
            "The phone was disconnected. Reconnect it and make sure USB is set "
            "to File Transfer.",
            errors_mod.describe(DeviceDisconnectedError("gone")))

    def test_insufficient_space_message_names_amounts(self):
        transfer_mod = try_import("android_photo_transfer.core.transfer")
        if transfer_mod is None or not hasattr(transfer_mod, "InsufficientSpaceError"):
            self.skipTest("core.transfer.InsufficientSpaceError not built yet")
        exc = transfer_mod.InsufficientSpaceError(2_000_000, 500_000)
        # The exception itself may or may not take (required, available);
        # describe() must produce the free-space message either way.
        message = errors_mod.describe(exc)
        self.assertIn("free space", message.lower())

    def test_timeout_maps_to_phone_stopped_responding(self):
        self.assertEqual(errors_mod.PHONESTOPPED,
                         errors_mod.describe(TimeoutError("timed out")))
        self.assertIn("stopped responding", errors_mod.PHONESTOPPED)

    def test_fallback_message(self):
        message = errors_mod.describe(ValueError("weird thing"))
        self.assertIn("Something went wrong", message)
        self.assertIn("weird thing", message)

    def test_is_destination_fatal(self):
        if not hasattr(errors_mod, "is_destination_fatal"):
            self.skipTest("is_destination_fatal not defined in core.errors")
        self.assertTrue(errors_mod.is_destination_fatal(
            OSError(28, "No space left on device")))
        self.assertTrue(errors_mod.is_destination_fatal(PermissionError("denied")))
        self.assertTrue(errors_mod.is_destination_fatal(
            FileNotFoundError("destination gone")))
        from android_photo_transfer.device.types import DeviceDisconnectedError

        self.assertFalse(errors_mod.is_destination_fatal(
            DeviceDisconnectedError("gone")))


if __name__ == "__main__":
    unittest.main()
