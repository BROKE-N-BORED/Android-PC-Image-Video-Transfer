"""Qt GUI smoke tests + thumbnail-cache LRU tests. XVFB ONLY.

Run: LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu
     TMPDIR=/tmp xvfb-run -a ~/workspace/forge-v2-testenv/bin/python
     -m unittest tests.test_gui_smoke -v
(or: tests/run_tests.py --gui)

Do NOT override HOME for these runs (Qt/xcb plugin init breaks under a
fake HOME; see AGENTS.md). Default HOME, TMPDIR=/tmp.

CONTRACT (ui/):
  - Main window 1220x780, minimum 960x580. Two tabs: "Photos and Videos"
    (thumbnail grid 160x186 tiles + list view toggle) and "Files".
  - No bare "&" in any visible label. No em-dashes in visible UI text.
    Select nothing by default.
  - Thumbnails: LRU memory cache, default 64 MB (clamp 16..1024); device
    thumbnail first, photo full-decode fallback for files <= 30 MB.

The main window is built with the same wiring as main.py (temp settings
+ history paths, demo-folder device manager, thumbnail manager, transfer
engine, MainViewModel), so these tests exercise the real startup path.
Tab titles may gain counts after a scan ("Photos and Videos  (3)"), so
the tab test matches on the title prefix.
"""
from __future__ import annotations

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import TempFolder, TestData, try_import  # noqa: E402

try:
    from PySide6.QtCore import Qt  # noqa: F401
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QListWidget, QTabWidget
    _QT_OK = True
except Exception:
    _QT_OK = False

WINDOW_CANDIDATES = (
    ("android_photo_transfer.ui.main_window", "MainWindow"),
    ("android_photo_transfer.ui.main", "MainWindow"),
    ("android_photo_transfer.main", "MainWindow"),
    ("android_photo_transfer.ui", "MainWindow"),
    ("android_photo_transfer.ui.main_window", "MainWindowView"),
)

CACHE_CANDIDATES = (
    ("android_photo_transfer.ui.thumbnails", "ThumbnailCache"),
    ("android_photo_transfer.core.thumbnails", "ThumbnailCache"),
    ("android_photo_transfer.ui.thumbnail_cache", "ThumbnailCache"),
    ("android_photo_transfer.core.thumbnail_cache", "ThumbnailCache"),
)

_app = None


def setUpModule():
    global _app
    if not _QT_OK:
        return
    inst = QApplication.instance()
    if inst is None:
        _app = QApplication(sys.argv)
    elif isinstance(inst, QApplication):
        # Reuse the application test_delete_safety already created.
        _app = inst
    else:
        # A plain QCoreApplication exists; widgets cannot be created.
        _app = None


def _find_main_window_class():
    for dotted, name in WINDOW_CANDIDATES:
        mod = try_import(dotted)
        cls = getattr(mod, name, None) if mod else None
        if isinstance(cls, type):
            return cls
    return None


def _find_cache_class():
    for dotted, name in CACHE_CANDIDATES:
        mod = try_import(dotted)
        cls = getattr(mod, name, None) if mod else None
        if isinstance(cls, type):
            return cls
    return None


def _visible_texts(widget):
    """All visible label/button/tab text in a widget tree."""
    from PySide6.QtWidgets import QTabWidget as _QTab

    texts = []
    for child in widget.findChildren(object):
        text = getattr(child, "text", None)
        if callable(text):
            try:
                value = child.text()
            except Exception:
                continue
            if isinstance(value, str) and value:
                texts.append(value)
        if isinstance(child, _QTab):
            for i in range(child.count()):
                texts.append(child.tabText(i))
    return texts


class GuiSmokeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not _QT_OK:
            raise unittest.SkipTest("PySide6 not importable; run with the Qt "
                                    "test python under xvfb-run -a")
        if _app is None:
            raise unittest.SkipTest("no QApplication available (a plain "
                                    "QCoreApplication is running instead)")
        cls.window_cls = _find_main_window_class()
        if cls.window_cls is None:
            raise unittest.SkipTest(
                "no MainWindow class found yet (tried: %s); UI not built"
                % ", ".join(f"{m}.{c}" for m, c in WINDOW_CANDIDATES))

    def setUp(self):
        self._folders: list[TempFolder] = []
        self._windows: list = []
        self._teardowns: list = []

    def tearDown(self):
        for fn in reversed(self._teardowns):
            try:
                fn()
            except Exception:
                pass
        for window in self._windows:
            try:
                window.close()
            except Exception:
                pass
        for folder in self._folders:
            folder.cleanup()
        if _app is not None:
            _app.processEvents()

    def _make_window(self, demo_folder=None):
        """Build the main window with the real main.py wiring.

        Settings + history go to temp paths (never the real HOME), and
        the device manager reads APT_DEMO_FOLDER at construction time.
        Returns (window, view_model).
        """
        from unittest import mock

        from android_photo_transfer.core.history import HistoryDatabase
        from android_photo_transfer.core.settings import SettingsManager
        from android_photo_transfer.core.transfer import TransferManager
        from android_photo_transfer.device.manager import DeviceManager
        from android_photo_transfer.ui.thumbnails import (
            ThumbnailCache,
            ThumbnailManager,
        )
        from android_photo_transfer.ui.viewmodels import MainViewModel

        tmp = TempFolder()
        self._folders.append(tmp)
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

        env = {"APT_DEMO_FOLDER": demo_folder} if demo_folder else {}
        with mock.patch.dict(os.environ, env):
            devices = DeviceManager(on_changed)
        view_model = MainViewModel(settings_manager, devices, thumbnails,
                                   transfer_engine, history)
        holder["vm"] = view_model
        try:
            window = self.window_cls(view_model, settings_manager, thumbnails)
        except TypeError:
            try:
                window = self.window_cls()
            except TypeError as exc:
                self.skipTest(f"MainWindow() takes unexpected constructor "
                              f"arguments ({exc}); GUI smoke test needs the "
                              f"main.py wiring (view_model, settings_manager, "
                              f"thumbnails)")
        self._windows.append(window)
        self._teardowns += [view_model.shutdown, devices.shutdown,
                            thumbnails.shutdown, history.close]
        return window, view_model

    def _demo_folder(self) -> str:
        folder = TempFolder()
        self._folders.append(folder)
        root = os.path.join(folder.path, "phone")
        os.makedirs(os.path.join(root, "DCIM", "Camera"))
        for name in ("IMG_1.jpg", "IMG_2.jpg", "VID_1.mp4"):
            with open(os.path.join(root, "DCIM", "Camera", name), "wb") as fh:
                fh.write(TestData.bytes(1000, len(name)))
        return root

    def test_window_opens_with_both_tabs(self):
        window, _vm = self._make_window()
        window.show()
        QTest.qWait(500)
        self.assertTrue(window.isVisible(), "main window did not become visible")
        self.assertGreaterEqual(window.width(), 960)
        self.assertGreaterEqual(window.height(), 580)
        tabs = window.findChildren(QTabWidget)
        self.assertTrue(tabs, "no QTabWidget found in the main window")
        titles = [tabs[0].tabText(i) for i in range(tabs[0].count())]
        # Titles gain counts after a scan ("Photos and Videos  (3)").
        self.assertEqual(2, len(titles))
        self.assertTrue(titles[0].startswith("Photos and Videos"), titles)
        self.assertTrue(titles[1].startswith("Files"), titles)

    def test_no_bare_ampersand_or_em_dash_in_visible_text(self):
        # Qt eats a bare & in labels (mnemonic marker) and the CONTRACT
        # bans em-dashes in visible text.
        window, _vm = self._make_window()
        window.show()
        QTest.qWait(300)
        for text in _visible_texts(window):
            with self.subTest(text=text):
                self.assertNotIn("&", text, "bare & in visible text")
                self.assertNotIn("\u2014", text, "em-dash in visible text")

    def test_nothing_selected_by_default(self):
        # "Select nothing by default" means no MEDIA is picked for
        # transfer: both libraries report zero selected, and the two
        # media views (grid + details list on each tab) hold no
        # selection. The category filter lists highlight their "All ..."
        # row and the Show combo shows "Show all" -- those are filters,
        # not media selections, so this test addresses the media views
        # by attribute instead of sweeping every list widget (which also
        # catches QComboBox popup views).
        window, view_model = self._make_window()
        window.show()
        QTest.qWait(300)
        self.assertEqual(0, view_model.media_library.selected_count)
        self.assertEqual(0, view_model.files_library.selected_count)
        media_views = [getattr(window, "photos_view", None),
                       getattr(window, "files_view", None)]
        self.assertTrue(any(v is not None for v in media_views),
                        "main window exposes no photos_view/files_view")
        for view in filter(None, media_views):
            grid = getattr(view, "grid_view", None)
            if grid is not None and grid.selectionModel() is not None:
                self.assertEqual([], grid.selectionModel().selectedIndexes(),
                                 "the media grid has a selection before the user clicked")
            table = getattr(view, "list_view", None)
            if table is not None:
                self.assertEqual([], table.selectedItems(),
                                 "the media list has a selection before the user clicked")

    def test_disabled_action_buttons_look_disabled(self):
        # A disabled button styled with a flat color keeps that color in
        # Qt (stylesheets beat the disabled palette), so Delete from
        # Phone looked enabled on the waiting screen while doing
        # nothing. Every action button that can be disabled must carry
        # a :disabled rule.
        window, _vm = self._make_window()
        window.show()
        QTest.qWait(300)
        from PySide6.QtWidgets import QPushButton
        buttons = {b.text(): b for b in window.findChildren(QPushButton)}
        delete = buttons.get("Delete from Phone")
        self.assertIsNotNone(delete, "no Delete from Phone button found")
        self.assertFalse(delete.isEnabled(),
                         "Delete must be disabled with no phone connected")
        self.assertIn(":disabled", delete.styleSheet(),
                      "Delete button has no :disabled rule, so it looks "
                      "enabled while disabled")
        transfer = buttons.get("TRANSFER")
        self.assertIsNotNone(transfer, "no TRANSFER button found")
        self.assertIn(":disabled", transfer.styleSheet(),
                      "TRANSFER button lost its :disabled rule")

    def test_folder_adapter_scan_populates_the_grid(self):
        # KNOWN PRODUCT BUG (2026-11-14, proven, not a test bug): this
        # test ERRORS against the current UI build. The tile delegate's
        # paint() reads QStyledItemDelegate.StateFlag (photos_view.py:116),
        # which does not exist in PySide6 6.12 -- the enum lives on QStyle
        # (QStyle.StateFlag.State_MouseOver). Painting any tile raises
        # AttributeError, which propagates out of the event loop into this
        # test. Fix (UI builder, one line): use QStyle.StateFlag. The
        # model itself populates fine (rowCount reaches 3); only painting
        # is broken, so on a real PC the Photos tab would show no tiles.
        #
        # The media grid is addressed by attribute (photos_view.grid_view):
        # sweeping every QListView/QListWidget would also match the Show
        # combo's popup (3 rows from the start) and the category lists
        # (populated before any scan), which would make this test pass
        # without any scan at all.
        root = self._demo_folder()
        window, view_model = self._make_window(demo_folder=root)
        window.show()
        grid = window.photos_view.grid_view
        self.assertEqual(0, grid.model().rowCount(),
                         "the grid already has rows before the scan starts")
        view_model.start()  # refresh + scan, as main.py does
        populated = False
        for _ in range(60):  # up to ~15 s
            QTest.qWait(250)
            if grid.model().rowCount() > 0:
                populated = True
                break
        self.assertTrue(
            populated,
            "the media grid stayed empty after ~15 s (APT_DEMO_FOLDER=%s)." % root)
        self.assertEqual(3, grid.model().rowCount(),
                         "the grid should show the 3 demo-folder files")


class ThumbnailCacheTests(unittest.TestCase):
    """LRU eviction. Mirrors upstream CoreTests.cs ThumbnailCacheTests.

    The cache stores QPixmaps in the port (upstream stored BitmapSource);
    these tests build small QPixmaps. The store method is `put` in the
    built cache (`add` in upstream); the tests accept either, and use
    len(cache) when no `count` attribute exists.
    """

    @classmethod
    def setUpClass(cls):
        if not _QT_OK:
            raise unittest.SkipTest("PySide6 not importable; run under xvfb-run -a")
        if _app is None:
            raise unittest.SkipTest("no QApplication available (a plain "
                                    "QCoreApplication is running instead)")
        cls.cache_cls = _find_cache_class()
        if cls.cache_cls is None:
            raise unittest.SkipTest(
                "no ThumbnailCache class found yet (tried: %s)"
                % ", ".join(f"{m}.{c}" for m, c in CACHE_CANDIDATES))

    def _image(self, size=100):
        from PySide6.QtGui import QColor, QPixmap

        pixmap = QPixmap(size, size)
        pixmap.fill(QColor("white"))
        return pixmap

    def _add(self, cache, key, size=100):
        put = getattr(cache, "put", getattr(cache, "add", None))
        if put is None:
            self.skipTest("ThumbnailCache has neither put() nor add()")
        put(key, self._image(size))

    def _count(self, cache):
        if hasattr(cache, "count"):
            return cache.count
        return len(cache)

    def test_evicts_least_recently_used_when_over_budget(self):
        one = 100 * 100 * 4
        cache = self.cache_cls(one * 3)
        self._add(cache, "a")
        self._add(cache, "b")
        self._add(cache, "c")
        self.assertIsNotNone(cache.get("a"))  # touch "a" so "b" is oldest
        self._add(cache, "d")
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNotNone(cache.get("c"))
        self.assertIsNotNone(cache.get("d"))
        if hasattr(cache, "current_bytes"):
            capacity = getattr(cache, "capacity_bytes", one * 3)
            self.assertLessEqual(cache.current_bytes, capacity)

    def test_shrinking_capacity_trims_immediately(self):
        cache = self.cache_cls(10_000_000)
        for i in range(10):
            self._add(cache, str(i))
        if not hasattr(cache, "capacity_bytes"):
            self.skipTest("ThumbnailCache has no capacity_bytes property")
        cache.capacity_bytes = 100 * 100 * 4 * 2
        self.assertEqual(2, self._count(cache))
        self.assertIsNone(cache.get("0"))
        self.assertIsNotNone(cache.get("9"))

    def test_oversized_item_is_refused_not_cached(self):
        cache = self.cache_cls(100 * 100 * 4)
        self._add(cache, "huge", size=1000)
        self.assertIsNone(cache.get("huge"))

    def test_clear_empties_the_cache(self):
        cache = self.cache_cls(10_000_000)
        self._add(cache, "a")
        if not hasattr(cache, "clear"):
            self.skipTest("ThumbnailCache has no clear()")
        cache.clear()
        self.assertIsNone(cache.get("a"))
        self.assertEqual(0, self._count(cache))


if __name__ == "__main__":
    unittest.main()
