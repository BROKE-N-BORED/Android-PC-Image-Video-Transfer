"""Delete-safety tests. The CONTRACT's delete fix (do NOT port the upstream
hole):

  - Delete from Phone by default deletes ONLY items already on the PC.
  - Items not yet copied are excluded and NAMED in the dialog.
  - Deleting untransferred items needs a SECOND explicit confirmation
    (checkbox + separate confirm dialog).

The rule lives in MainViewModel (ui/viewmodels.py):
  - ask_to_delete(items) emits delete_dialog_requested with
    {"items", "on_pc", "not_on_pc", "device_name", "total_bytes"}.
  - confirm_delete(items, include_untransferred) drops untransferred
    items unless the second confirmation passed include_untransferred
    True, then deletes on a worker thread.

These tests drive that real view model against a demo-folder device and
check what actually disappears from disk. They need PySide6 (QObjects,
QThread) but no window: they run under a QApplication (offscreen when no
display is set, so they also run in the plain engine suite when the Qt
python is used), and skip cleanly under a Python without PySide6. The
application object is deliberately a QApplication, not a QCoreApplication,
so the GUI smoke tests in test_gui_smoke.py (which run after this module
alphabetically) can reuse it for widgets.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

if not os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from fakes import FakeAdapter, TempFolder, TestData, try_import  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    _QT_OK = True
except Exception:
    _QT_OK = False

viewmodels_mod = try_import("android_photo_transfer.ui.viewmodels")
MainViewModel = getattr(viewmodels_mod, "MainViewModel", None) if viewmodels_mod else None
MediaItemVM = getattr(viewmodels_mod, "MediaItemVM", None) if viewmodels_mod else None

_app = None


def setUpModule():
    global _app
    if _QT_OK:
        _app = QApplication.instance() or QApplication(sys.argv)


def _pump(seconds: float, until=None):
    """Process Qt events until `until()` is true or time runs out."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if until is not None and until():
            return True
        if _app is not None:
            _app.processEvents()
        time.sleep(0.05)
    return until() if until is not None else False


class DeleteSafetyViewModelTests(unittest.TestCase):
    """The safety rule, through the real MainViewModel + demo folder."""

    @classmethod
    def setUpClass(cls):
        if not _QT_OK:
            raise unittest.SkipTest("PySide6 not importable; run with the Qt test python")
        if MainViewModel is None or MediaItemVM is None:
            raise unittest.SkipTest("ui.viewmodels.MainViewModel not built yet")

    def setUp(self):
        from unittest import mock

        from android_photo_transfer.core.history import HistoryDatabase
        from android_photo_transfer.core.scanner import scan as scan_fn
        from android_photo_transfer.core.settings import SettingsManager
        from android_photo_transfer.core.transfer import TransferManager
        from android_photo_transfer.device.folder_adapter import FolderAdapter
        from android_photo_transfer.device.manager import DeviceManager
        from android_photo_transfer.ui.thumbnails import (
            ThumbnailCache,
            ThumbnailManager,
        )

        self._folders: list[TempFolder] = []
        tmp = TempFolder()
        self._folders.append(tmp)

        # Demo phone on disk: one already-copied photo, two new files.
        self.demo_root = os.path.join(tmp.path, "phone")
        camera = os.path.join(self.demo_root, "DCIM", "Camera")
        os.makedirs(camera)
        self.paths = {}
        for name, seed in (("copied.jpg", 1), ("new1.jpg", 2), ("new2.mp4", 3)):
            path = os.path.join(camera, name)
            with open(path, "wb") as fh:
                fh.write(TestData.bytes(500, seed))
            self.paths[name] = path

        settings_manager = SettingsManager(os.path.join(tmp.path, "settings.json"))
        history = HistoryDatabase(os.path.join(tmp.path, "history.db"))
        cache = ThumbnailCache(64 * 1024 * 1024)
        thumbnails = ThumbnailManager(cache)
        transfer_engine = TransferManager(history)
        holder: dict = {}

        def on_changed():
            vm = holder.get("vm")
            if vm is not None:
                vm.on_device_manager_changed()

        with mock.patch.dict(os.environ, {"APT_DEMO_FOLDER": self.demo_root}):
            devices = DeviceManager(on_changed)
        self.vm = MainViewModel(settings_manager, devices, thumbnails,
                                transfer_engine, history)
        holder["vm"] = self.vm
        self._shutdowns = [self.vm.shutdown, devices.shutdown,
                           thumbnails.shutdown, history.close]
        devices.refresh()
        # Sync the view model with the connected demo phone directly
        # (normally reached via the device_changed signal).
        self.vm.sync_device_state()
        self.assertIsNotNone(getattr(self.vm, "_active_device", None),
                             "view model did not pick up the demo phone")

        # Real MediaItems for the demo files, via the real scanner.
        adapter = FolderAdapter(self.demo_root)
        found: list = []
        try:
            scan_fn(adapter, devices.current.info.device_id,
                    found.extend, lambda: False)
        finally:
            adapter.close()
        self.assertEqual(3, len(found), "demo folder scan found %d files" % len(found))
        self.items = {item.name: item for item in found}

    def tearDown(self):
        for fn in self._shutdowns:
            try:
                fn()
            except Exception:
                pass
        for folder in self._folders:
            folder.cleanup()
        _pump(0.2)

    def _wrap(self, name, already_transferred):
        item = self.items[name]
        item.already_transferred = already_transferred
        return MediaItemVM(item, lambda vm, value: None)

    def _mixed_selection(self):
        return [self._wrap("copied.jpg", True),
                self._wrap("new1.jpg", False),
                self._wrap("new2.mp4", False)]

    def test_ask_to_delete_names_on_pc_and_not_on_pc(self):
        payloads: list = []
        self.vm.delete_dialog_requested.connect(payloads.append)
        self.vm.ask_to_delete(self._mixed_selection())
        self.assertEqual(1, len(payloads), "no delete dialog was requested")
        payload = payloads[0]
        self.assertEqual({"copied.jpg"}, {v.name for v in payload["on_pc"]})
        # Blocked items are NAMED, so the dialog can list them.
        self.assertEqual({"new1.jpg", "new2.mp4"},
                         {v.name for v in payload["not_on_pc"]})
        self.assertEqual(3, len(payload["items"]))

    def test_ask_to_delete_with_everything_transferred_blocks_nothing(self):
        payloads: list = []
        self.vm.delete_dialog_requested.connect(payloads.append)
        self.vm.ask_to_delete([self._wrap("copied.jpg", True),
                               self._wrap("new1.jpg", True)])
        self.assertEqual([], payloads[0]["not_on_pc"])
        self.assertEqual(2, len(payloads[0]["on_pc"]))

    def test_ask_to_delete_with_nothing_transferred_blocks_everything(self):
        payloads: list = []
        self.vm.delete_dialog_requested.connect(payloads.append)
        self.vm.ask_to_delete([self._wrap("new1.jpg", False)])
        self.assertEqual([], payloads[0]["on_pc"])
        self.assertEqual(["new1.jpg"], [v.name for v in payloads[0]["not_on_pc"]])

    def test_confirm_delete_without_second_confirmation_deletes_only_on_pc(self):
        # KNOWN PRODUCT BUG (2026-11-14, proven, not a test bug): this
        # test FAILS against the current UI build. MainViewModel.
        # confirm_delete creates the _DeleteWorker as a bare local
        # (viewmodels.py:1606) -- every other worker in the file is stored
        # on self (self._worker, self._scan_worker, self._device_worker).
        # The worker is garbage-collected when confirm_delete returns, so
        # thread.started -> worker.run never fires, nothing is deleted,
        # and the thread leaks. Fix (UI builder, one line): store it as
        # self._delete_worker in confirm_delete (and clear it in
        # _on_delete_finished, mirroring the transfer worker). Verified
        # by holding a reference in a probe: the delete then works
        # exactly as designed (on-PC file gone, new files kept).
        self.vm.confirm_delete(self._mixed_selection(), include_untransferred=False)
        gone = _pump(10, until=lambda: not os.path.exists(self.paths["copied.jpg"]))
        self.assertTrue(gone, "the on-PC file was not deleted")
        self.assertTrue(os.path.exists(self.paths["new1.jpg"]),
                        "untransferred file deleted without the second confirmation")
        self.assertTrue(os.path.exists(self.paths["new2.mp4"]),
                        "untransferred file deleted without the second confirmation")

    def test_confirm_delete_with_second_confirmation_deletes_everything(self):
        # Same known product bug as the test above (unreferenced
        # _DeleteWorker is garbage-collected): fails until the UI builder
        # stores the worker on self.
        self.vm.confirm_delete(self._mixed_selection(), include_untransferred=True)
        gone = _pump(10, until=lambda: not any(
            os.path.exists(p) for p in self.paths.values()))
        self.assertTrue(gone, "not all files were deleted after the second confirmation")

    def test_confirm_delete_with_nothing_on_pc_deletes_nothing(self):
        selection = [self._wrap("new1.jpg", False), self._wrap("new2.mp4", False)]
        self.vm.confirm_delete(selection, include_untransferred=False)
        _pump(1.0)
        self.assertTrue(os.path.exists(self.paths["new1.jpg"]))
        self.assertTrue(os.path.exists(self.paths["new2.mp4"]))


class AdapterDeleteTests(unittest.TestCase):
    """The engine-level delete the safety rule gates (no Qt needed)."""

    def test_delete_removes_only_the_named_file(self):
        phone = FakeAdapter()
        copied = phone.add_file("DCIM/Camera/copied.jpg", TestData.bytes(10))
        kept = phone.add_file("DCIM/Camera/kept.jpg", TestData.bytes(10))
        phone.delete_file(copied.source_path)
        self.assertEqual(1, phone.file_count())
        entries: list = []
        phone.list_folder("Internal shared storage/DCIM/Camera", entries.extend)
        self.assertEqual(["kept.jpg"], [e.name for e in entries])
        # The surviving file is still fully readable.
        with phone.open_read(kept.source_path, kept.persistent_id) as stream:
            self.assertEqual(TestData.bytes(10), stream.read())


if __name__ == "__main__":
    unittest.main()
