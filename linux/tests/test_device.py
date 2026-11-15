"""Device layer tests: FakeAdapter conformance, folder adapter round trip,
DeviceManager behaviour, and mocked-ctypes libmtp backend tests.

CONTRACT (device/interface.py, device/manager.py):
  - DeviceAdapter ABC: info, is_connected, get_storage_roots,
    folder_has_entries, list_folder (batches <= 200), open_read,
    get_thumbnail, read_all_bytes (ValueError over max_bytes),
    delete_file, is_still_connected, close. Module-level list_phones()
    and connect(device_id) per backend. DeviceDisconnectedError when the
    device is gone.
  - DeviceManager(on_changed): state, phones, current, storage_roots,
    error_message; refresh() lists phones via libmtp, appends a
    folder-adapter phone ("demo:<abspath>", "Demo Phone (folder)") when
    APT_DEMO_FOLDER names an existing dir, auto-connects, probes roots
    (any readable root -> READY, else LOCKED with 3 s retry timer);
    reconnect(), select_phone(device_id), shutdown(); 30 s connect timeout.

Module names for the folder adapter / libmtp backend / manager are not
pinned by the CONTRACT, so these tests probe the likely module paths and
skip with a clear note when a module is not built yet.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import FakeAdapter, TempFolder, TestData, try_import  # noqa: E402
from android_photo_transfer.device.interface import DeviceAdapter  # noqa: E402
from android_photo_transfer.device.types import (  # noqa: E402
    DeviceDisconnectedError,
)

FOLDER_CANDIDATES = (
    "android_photo_transfer.device.folder",
    "android_photo_transfer.device.folder_adapter",
    "android_photo_transfer.device.local_folder",
)
LIBMTP_CANDIDATES = (
    "android_photo_transfer.device.libmtp_backend",
    "android_photo_transfer.device.libmtp",
    "android_photo_transfer.device.mtp",
    "android_photo_transfer.device.backend_libmtp",
)
MANAGER_CANDIDATES = (
    "android_photo_transfer.device.manager",
    "android_photo_transfer.device.device_manager",
)


def _first_module(candidates):
    for dotted in candidates:
        mod = try_import(dotted)
        if mod is not None:
            return dotted, mod
    return None, None


def _adapter_class(mod):
    """The DeviceAdapter subclass in a backend module, if any."""
    if mod is None:
        return None
    for name in ("FolderAdapter", "LocalFolderAdapter", "LibmtpAdapter",
                 "MtpAdapter", "LibMTPAdapter"):
        cls = getattr(mod, name, None)
        if isinstance(cls, type) and issubclass(cls, DeviceAdapter):
            return cls
    for value in vars(mod).values():
        if isinstance(value, type) and issubclass(value, DeviceAdapter) \
                and value is not DeviceAdapter:
            return value
    return None


class FakeAdapterConformanceTests(unittest.TestCase):
    """The fake itself must honour the DeviceAdapter contract."""

    def test_is_a_device_adapter(self):
        self.assertIsInstance(FakeAdapter(), DeviceAdapter)

    def test_storage_roots_and_folder_entries(self):
        phone = FakeAdapter()
        roots = phone.get_storage_roots()
        self.assertEqual(1, len(roots))
        self.assertEqual("Internal shared storage", roots[0].name)
        self.assertFalse(phone.folder_has_entries(roots[0].path))
        phone.add_file("DCIM/a.jpg", TestData.bytes(10))
        self.assertTrue(phone.folder_has_entries(roots[0].path))
        self.assertTrue(phone.folder_has_entries(
            "Internal shared storage/DCIM"))
        self.assertFalse(phone.folder_has_entries(
            "Internal shared storage/Nope"))

    def test_list_folder_batches_are_at_most_200_entries(self):
        phone = FakeAdapter()
        for i in range(450):
            phone.add_file(f"DCIM/f{i:03d}.jpg", TestData.bytes(5))
        batches: list[list] = []
        phone.list_folder("Internal shared storage/DCIM", batches.append)
        self.assertEqual(450, sum(len(b) for b in batches))
        for batch in batches:
            self.assertLessEqual(len(batch), 200)

    def test_open_read_by_id_and_by_path(self):
        phone = FakeAdapter()
        item = phone.add_file("DCIM/a.jpg", TestData.bytes(100, 3))
        with phone.open_read(item.source_path, item.persistent_id) as stream:
            self.assertEqual(TestData.bytes(100, 3), stream.read())
        with phone.open_read(item.source_path, None) as stream:
            self.assertEqual(TestData.bytes(100, 3), stream.read())
        self.assertEqual(["id", "path"], phone.opened_by)

    def test_read_all_bytes_enforces_max_bytes(self):
        phone = FakeAdapter()
        item = phone.add_file("DCIM/a.jpg", TestData.bytes(1000, 4))
        self.assertEqual(TestData.bytes(1000, 4),
                         phone.read_all_bytes(item.source_path, None, 2000))
        with self.assertRaises(ValueError):
            phone.read_all_bytes(item.source_path, None, 999)

    def test_delete_file_removes_it_from_tree_and_listing(self):
        phone = FakeAdapter()
        item = phone.add_file("DCIM/a.jpg", TestData.bytes(10))
        phone.add_file("DCIM/b.jpg", TestData.bytes(10))
        phone.delete_file(item.source_path)
        self.assertEqual(1, phone.file_count())
        entries: list = []
        phone.list_folder("Internal shared storage/DCIM", entries.extend)
        self.assertEqual(["b.jpg"], [e.name for e in entries])
        with self.assertRaises(FileNotFoundError):
            phone.delete_file(item.source_path)

    def test_disconnected_phone_raises_on_every_call(self):
        phone = FakeAdapter()
        item = phone.add_file("DCIM/a.jpg", TestData.bytes(10))
        phone.connected = False
        self.assertFalse(phone.is_connected)
        self.assertFalse(phone.is_still_connected())
        with self.assertRaises(DeviceDisconnectedError):
            phone.list_folder("Internal shared storage/DCIM", lambda b: None)
        with self.assertRaises(DeviceDisconnectedError):
            phone.open_read(item.source_path, None)
        with self.assertRaises(DeviceDisconnectedError):
            phone.delete_file(item.source_path)

    def test_thumbnails_default_to_none(self):
        phone = FakeAdapter()
        item = phone.add_file("DCIM/a.jpg", TestData.bytes(10))
        self.assertIsNone(phone.get_thumbnail(item.source_path, None))
        phone.set_thumbnail(item.source_path, b"\x89PNG...")
        self.assertEqual(b"\x89PNG...",
                         phone.get_thumbnail(item.source_path, None))

    def test_stream_reads_cap_at_64k_like_mtp(self):
        phone = FakeAdapter()
        item = phone.add_file("DCIM/a.jpg", TestData.bytes(200_000, 5))
        with phone.open_read(item.source_path, None) as stream:
            first = stream.read(1024 * 1024)
        self.assertEqual(64 * 1024, len(first))


class FolderAdapterTests(unittest.TestCase):
    """Round trip against a real folder (upstream LocalFolderAdapter)."""

    def setUp(self):
        _dotted, self.mod = _first_module(FOLDER_CANDIDATES)
        self.cls = _adapter_class(self.mod)
        if self.cls is None:
            self.skipTest("folder adapter module not built yet (tried: %s)"
                          % ", ".join(FOLDER_CANDIDATES))
        self._folders: list[TempFolder] = []

    def tearDown(self):
        for folder in self._folders:
            folder.cleanup()

    def _phone_dir(self) -> str:
        folder = TempFolder()
        self._folders.append(folder)
        root = os.path.join(folder.path, "phone")
        os.makedirs(os.path.join(root, "DCIM", "Camera"))
        os.makedirs(os.path.join(root, "Pictures"))
        with open(os.path.join(root, "DCIM", "Camera", "IMG_1.jpg"), "wb") as fh:
            fh.write(TestData.bytes(1000, 7))
        with open(os.path.join(root, "Pictures", "pic.png"), "wb") as fh:
            fh.write(TestData.bytes(500, 8))
        return root

    def _connect(self, root):
        import inspect

        connect = getattr(self.mod, "connect", None)
        list_phones = getattr(self.mod, "list_phones", None)
        if connect is not None and list_phones is not None:
            try:
                params = inspect.signature(list_phones).parameters
            except (TypeError, ValueError):
                params = {}
            # Some backends list from the environment, others take the
            # folder as an argument; call it whichever way it wants.
            listing = list_phones(root) if params else list_phones()
            for phone in listing:
                if root in phone.device_id:
                    return connect(phone.device_id)
        if connect is not None:
            return connect("demo:" + os.path.abspath(root))
        return self.cls(root)

    def test_round_trip_list_read_delete(self):
        root = self._phone_dir()
        adapter = self._connect(root)
        try:
            self.assertTrue(adapter.is_connected)
            self.assertIn("Demo Phone", adapter.info.name)
            self.assertTrue(adapter.info.device_id.startswith("demo:"))
            roots = adapter.get_storage_roots()
            self.assertEqual(1, len(roots))
            entries: list = []
            adapter.list_folder(os.path.join(root, "DCIM", "Camera"),
                                entries.extend)
            self.assertEqual(["IMG_1.jpg"], [e.name for e in entries])
            self.assertEqual(1000, entries[0].size)
            self.assertIsNotNone(entries[0].persistent_id)
            with adapter.open_read(entries[0].full_name,
                                   entries[0].persistent_id) as stream:
                self.assertEqual(TestData.bytes(1000, 7), stream.read())
            self.assertEqual(
                TestData.bytes(1000, 7),
                adapter.read_all_bytes(entries[0].full_name, None, 10_000))
            with self.assertRaises(ValueError):
                adapter.read_all_bytes(entries[0].full_name, None, 999)
            adapter.delete_file(entries[0].full_name)
            self.assertFalse(os.path.exists(
                os.path.join(root, "DCIM", "Camera", "IMG_1.jpg")))
        finally:
            adapter.close()

    def test_folder_adapter_lists_phones_and_connects(self):
        root = self._phone_dir()
        list_phones = getattr(self.mod, "list_phones", None)
        connect = getattr(self.mod, "connect", None)
        if list_phones is None or connect is None:
            self.skipTest("folder module has no list_phones/connect helpers")
        # The folder backend lists phones from APT_DEMO_FOLDER (or takes
        # the folder as its device id); either way connect("demo:<root>")
        # must work.
        adapter = connect("demo:" + root)
        try:
            self.assertTrue(adapter.is_connected)
            self.assertEqual(1, len(adapter.get_storage_roots()))
        finally:
            adapter.close()


class DeviceManagerTests(unittest.TestCase):
    """DeviceManager with APT_DEMO_FOLDER (no real phone, no libmtp)."""

    def setUp(self):
        _dotted, self.mod = _first_module(MANAGER_CANDIDATES)
        self.manager_cls = getattr(self.mod, "DeviceManager", None) if self.mod else None
        if self.manager_cls is None:
            self.skipTest("device manager module not built yet (tried: %s)"
                          % ", ".join(MANAGER_CANDIDATES))
        self._folders: list[TempFolder] = []
        self._managers: list = []

    def tearDown(self):
        for manager in self._managers:
            try:
                manager.shutdown()
            except Exception:
                pass
        for folder in self._folders:
            folder.cleanup()

    def _demo_folder(self, with_files=True) -> str:
        folder = TempFolder()
        self._folders.append(folder)
        root = os.path.join(folder.path, "phone")
        os.makedirs(os.path.join(root, "DCIM", "Camera"), exist_ok=True)
        if with_files:
            with open(os.path.join(root, "DCIM", "Camera", "IMG_1.jpg"), "wb") as fh:
                fh.write(TestData.bytes(100, 9))
        return root

    def _manager(self, demo_folder=None):
        """Build a manager with APT_DEMO_FOLDER set at construction time.

        DeviceManager reads the variable in __init__ (it is process-level
        config, read once at startup), so the env patch must wrap the
        construction, not just refresh().
        """
        from unittest import mock

        changed = threading.Event()
        env = {"APT_DEMO_FOLDER": demo_folder} if demo_folder else {}
        with mock.patch.dict(os.environ, env):
            manager = self.manager_cls(lambda: changed.set())
        self._managers.append(manager)
        return manager, changed

    def test_demo_folder_phone_appears_and_reaches_ready(self):
        root = self._demo_folder()
        manager, changed = self._manager(demo_folder=root)
        manager.refresh()
        self.assertTrue(changed.is_set(), "on_changed was not called by refresh()")
        names = [p.name for p in manager.phones]
        self.assertIn("Demo Phone (folder)", names)
        demo = [p for p in manager.phones if p.name == "Demo Phone (folder)"][0]
        self.assertEqual("demo:" + os.path.abspath(root), demo.device_id)
        self.assertIsNotNone(manager.current, "manager did not auto-connect")
        self.assertTrue(manager.storage_roots, "no storage roots after connect")
        from android_photo_transfer.device.types import ConnectionState

        self.assertEqual(ConnectionState.READY, manager.state)

    def test_empty_demo_folder_is_locked_not_ready(self):
        from android_photo_transfer.device.types import ConnectionState

        # Completely empty: not even subfolders. (A folder containing an
        # empty DCIM/Camera still "has entries" and reads as READY.)
        folder = TempFolder()
        self._folders.append(folder)
        root = os.path.join(folder.path, "phone")
        os.makedirs(root)
        manager, _changed = self._manager(demo_folder=root)
        manager.refresh()
        self.assertEqual(ConnectionState.LOCKED, manager.state)
        # Locked means connected-but-unreadable: the adapter is attached,
        # only its storage cannot be read yet. (error_message stays for
        # the ERROR state; the locked wording comes from state + device
        # name in the UI, per CONTRACT.)
        self.assertIsNotNone(manager.current)

    def test_missing_demo_folder_is_ignored(self):
        from android_photo_transfer.device.types import ConnectionState

        manager, _changed = self._manager(demo_folder="/nonexistent/apt-demo")
        manager.refresh()
        names = [p.name for p in manager.phones]
        self.assertNotIn("Demo Phone (folder)", names)
        self.assertIn(manager.state, (ConnectionState.WAITING, ConnectionState.ERROR))

    def test_reconnect_and_shutdown_do_not_raise(self):
        root = self._demo_folder()
        manager, _changed = self._manager(demo_folder=root)
        manager.refresh()
        manager.reconnect()
        manager.shutdown()
        self._managers.remove(manager)


class LibmtpBackendTests(unittest.TestCase):
    """libmtp backend with ctypes mocked: no real libmtp, no real phone.

    Skipped entirely when the backend module is not built yet.
    """

    def setUp(self):
        _dotted, self.mod = _first_module(LIBMTP_CANDIDATES)
        if self.mod is None:
            self.skipTest("libmtp backend module not built yet (tried: %s); "
                          "real-device behaviour still needs a test on the "
                          "target PC with a phone plugged in"
                          % ", ".join(LIBMTP_CANDIDATES))

    def _reset_lib_cache(self):
        """Forget the backend's cached libmtp handle (restored by caller)."""
        saved = {}
        for name in ("_lib", "_lib_tried", "_init_done"):
            saved[name] = getattr(self.mod, name, None)
            setattr(self.mod, name, False if name != "_lib" else None)
        return saved

    def _restore_lib_cache(self, saved):
        for name, value in saved.items():
            setattr(self.mod, name, value)

    def test_module_imports_cleanly_without_libmtp(self):
        # Importing the backend must never fail (lazy library loading):
        # setUp already imported it, so reaching here proves it.
        self.assertTrue(callable(getattr(self.mod, "list_phones", None)))
        self.assertTrue(callable(getattr(self.mod, "connect", None)))

    def test_list_phones_without_libmtp_returns_empty_or_raises_device_error(self):
        from unittest import mock

        from android_photo_transfer.device.types import DeviceError

        list_phones = getattr(self.mod, "list_phones", None)
        if list_phones is None:
            self.skipTest("libmtp module has no list_phones()")
        saved = self._reset_lib_cache()
        try:
            with mock.patch("ctypes.util.find_library", return_value=None), \
                    mock.patch("ctypes.CDLL", side_effect=OSError("no libmtp here")):
                try:
                    phones = list_phones()
                except (DeviceError, DeviceDisconnectedError, OSError):
                    return  # raising a device error is acceptable too
                self.assertEqual([], phones)
        finally:
            self._restore_lib_cache(saved)

    def test_connect_without_libmtp_raises_a_device_error(self):
        from unittest import mock

        from android_photo_transfer.device.types import DeviceError

        connect = getattr(self.mod, "connect", None)
        if connect is None:
            self.skipTest("libmtp module has no connect()")
        saved = self._reset_lib_cache()
        try:
            with mock.patch("ctypes.util.find_library", return_value=None), \
                    mock.patch("ctypes.CDLL", side_effect=OSError("no libmtp here")):
                with self.assertRaises((DeviceError, DeviceDisconnectedError, OSError)):
                    connect("mtp:1:2")
        finally:
            self._restore_lib_cache(saved)

    def test_backend_exposes_device_adapter_subclass(self):
        cls = _adapter_class(self.mod)
        if cls is None:
            self.skipTest("libmtp module exposes no DeviceAdapter subclass")
        self.assertTrue(issubclass(cls, DeviceAdapter))


if __name__ == "__main__":
    unittest.main()
