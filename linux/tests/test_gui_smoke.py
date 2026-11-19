"""Qt GUI smoke tests + thumbnail-cache LRU tests + updater UI tests.
XVFB ONLY.

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

The updater tests (UpdateGuiTests) monkeypatch
android_photo_transfer.core.updates.check_for_update with canned
results, so no network is ever touched. Modal dialogs are driven by a
watchdog QTimer (_DialogWatchdog): timers keep firing inside a modal
exec() loop, so the watchdog can inspect and dismiss a dialog the GUI
thread is blocked on. Never call a modal-blocking function from inside
the watchdog callback -- a QTimer does not re-fire while its own timeout
handler is on the stack. Fire blocking calls via QTimer.singleShot from
the main loop instead (see test_manual_check_while_running_is_refused).
"""
from __future__ import annotations

import os
import sys
import time
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import TempFolder, TestData, try_import  # noqa: E402

try:
    from PySide6.QtCore import Qt, QTimer  # noqa: F401
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QListWidget,
        QMessageBox,
        QPushButton,
        QTabWidget,
    )
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


# --- updater-test helpers -------------------------------------------------
# Canned check_for_update results. The version 9.9.9 is deliberately
# far above any real release, so "newer" is unambiguous.

FAKE_NEWER = {
    "state": "newer", "version": "9.9.9", "tag": "v9.9.9",
    "zip_url": "https://example.invalid/x.zip",
    "sidecar_url": "https://example.invalid/x.zip.sha256",
    "page_url": "https://example.invalid/release",
    "latest": "9.9.9",
}
FAKE_CURRENT = {"state": "current", "latest": "0.1.1"}
FAKE_UNKNOWN = {"state": "unknown", "latest": None,
                "reason": "Could not reach GitHub: test failure"}


def _visible_dialogs():
    """Visible top-level dialogs (QMessageBox and UpdateDialog alike)."""
    return [w for w in QApplication.topLevelWidgets()
            if isinstance(w, QDialog) and w.isVisible()]


def _click_button(dialog, text):
    """Click the visible button with this text; False when there is none."""
    for button in dialog.findChildren(QPushButton):
        if button.text() == text and button.isVisible():
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            return True
    return False


def _dialog_texts(dialog):
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in dialog.findChildren(QLabel)]


class _DialogWatchdog:
    """Polls for visible modal dialogs while the GUI thread sits inside
    a modal exec() loop, and hands each new dialog to `handler` once.

    The handler records what it saw and dismisses the dialog; it must
    NOT assert (an exception inside a Qt slot is swallowed, not
    reported) and must NOT call anything modal itself -- a QTimer does
    not re-fire while its own timeout handler is on the stack. The test
    asserts on the recorded data after the wait loop returns.
    """

    def __init__(self, handler):
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._handler = handler
        self._seen_ids: set[int] = set()

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        for dialog in _visible_dialogs():
            if id(dialog) in self._seen_ids:
                continue
            self._seen_ids.add(id(dialog))
            self._handler(dialog)


class _MainWindowTestCase(unittest.TestCase):
    """Shared harness: Qt + MainWindow lookup, temp settings/history,
    and the real main.py window wiring. The smoke tests and the updater
    tests both build on it; it holds no tests of its own."""

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


class GuiSmokeTests(_MainWindowTestCase):
    """Window layout, visible-text rules, default selection, and the
    demo-folder scan."""

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


class UpdateGuiTests(_MainWindowTestCase):
    """Updater UI (v0.1.1): silent + manual checks, the update dialog,
    and the Help menu startup toggle.

    updates.check_for_update is monkeypatched with canned results, so
    no network is ever touched; every patch is restored in tearDown
    via addCleanup. Modal dialogs are driven by _DialogWatchdog (see
    the module docstring for the trap note).
    """

    def _patch_check(self, fake):
        """Replace updates.check_for_update with a canned result (a dict)
        or a callable; restored automatically after the test."""
        from unittest import mock

        from android_photo_transfer.core import updates

        if callable(fake):
            new = fake
        else:
            new = lambda _version, _fake=fake: dict(_fake)  # noqa: E731
        patcher = mock.patch.object(updates, "check_for_update", new)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _help_action(self, window, text):
        from PySide6.QtWidgets import QMenu

        menus = [m for m in window.menuBar().findChildren(QMenu)]
        help_menu = next((m for m in menus if m.title() == "Help"), None)
        self.assertIsNotNone(help_menu, "no Help menu in the menu bar")
        for action in help_menu.actions():
            if action.text() == text:
                return action
        self.fail(f"no Help menu action {text!r}: "
                  f"{[a.text() for a in help_menu.actions()]}")

    def _wait_until(self, condition, timeout_s=15):
        """Pump the event loop until condition() holds or the timeout
        passes. Returns True when the condition held in time."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if condition():
                return True
            QTest.qWait(100)
        return condition()

    def _wait_for_check_done(self, window, timeout_s=15):
        """The check thread is cleared in _on_update_check_finished, so
        a None thread means the check's result has been handled."""
        return self._wait_until(
            lambda: window._update_check_thread is None, timeout_s)

    def _show_window(self):
        window, _vm = self._make_window()
        window.show()
        QTest.qWait(300)
        return window

    def test_silent_check_newer_shows_update_dialog(self):
        from android_photo_transfer import __version__ as current
        from android_photo_transfer.ui.updates_ui import UpdateDialog

        window = self._show_window()
        self._patch_check(FAKE_NEWER)
        found: dict = {}

        def handle(dialog):
            if isinstance(dialog, UpdateDialog):
                found["texts"] = _dialog_texts(dialog)
                found["buttons"] = [b.text()
                                    for b in dialog.findChildren(QPushButton)]
                found["title"] = dialog.windowTitle()
                found["closed"] = _click_button(dialog, "Close")

        watchdog = _DialogWatchdog(handle)
        watchdog.start()
        self.addCleanup(watchdog.stop)
        window.start_silent_update_check()
        self.assertTrue(
            self._wait_until(lambda: "texts" in found),
            "no update dialog appeared within 15 s")
        watchdog.stop()
        self.assertTrue(found.get("closed"), "the dialog had no Close button")
        self.assertEqual("Update available", found["title"])
        body = " ".join(found["texts"])
        self.assertIn("Android Photo Transfer 9.9.9 is on GitHub", body)
        self.assertIn(f"You have {current}", body)
        # Source run: no download/apply, just the release page.
        self.assertIn("Open the release page", found["buttons"])
        self.assertIn("Close", found["buttons"])

    def test_silent_check_up_to_date_shows_nothing(self):
        window = self._show_window()
        self._patch_check(FAKE_CURRENT)
        before = window._status_label.text()
        window.start_silent_update_check()
        self.assertTrue(self._wait_for_check_done(window),
                        "the silent check never finished")
        QTest.qWait(500)  # give any stray modal time to appear
        self.assertEqual([], _visible_dialogs(),
                         "the silent up-to-date check showed a dialog")
        self.assertEqual(before, window._status_label.text(),
                         "the silent up-to-date check changed the status bar")

    def test_silent_check_failure_shows_no_modal(self):
        window = self._show_window()
        self._patch_check(FAKE_UNKNOWN)
        window.start_silent_update_check()
        self.assertTrue(self._wait_for_check_done(window),
                        "the silent check never finished")
        QTest.qWait(500)  # give any stray modal time to appear
        self.assertEqual([], _visible_dialogs(),
                         "the silent failed check showed a modal")
        # A status-bar line at most, never a modal.
        self.assertEqual("Could not check for updates.",
                         window._status_label.text())

    def test_manual_check_up_to_date_shows_info_dialog(self):
        window = self._show_window()
        self._patch_check(FAKE_CURRENT)
        found: dict = {}

        def handle(dialog):
            if isinstance(dialog, QMessageBox):
                found["text"] = dialog.text()
                found["title"] = dialog.windowTitle()
                found["icon"] = dialog.icon()
                found["closed"] = _click_button(dialog, "OK")

        watchdog = _DialogWatchdog(handle)
        watchdog.start()
        self.addCleanup(watchdog.stop)
        self._help_action(window, "Check for Updates...").trigger()
        self.assertTrue(
            self._wait_until(lambda: "text" in found),
            "no up-to-date dialog appeared within 15 s")
        watchdog.stop()
        self.assertTrue(found.get("closed"), "the dialog had no OK button")
        self.assertEqual("Check for Updates", found["title"])
        self.assertEqual(QMessageBox.Icon.Information, found["icon"])
        self.assertIn("You have the latest version.", found["text"])
        self.assertIn("0.1.1", found["text"])

    def test_manual_check_failure_shows_warning_with_reason(self):
        window = self._show_window()
        self._patch_check(FAKE_UNKNOWN)
        found: dict = {}

        def handle(dialog):
            if isinstance(dialog, QMessageBox):
                found["text"] = dialog.text()
                found["icon"] = dialog.icon()
                found["closed"] = _click_button(dialog, "OK")

        watchdog = _DialogWatchdog(handle)
        watchdog.start()
        self.addCleanup(watchdog.stop)
        self._help_action(window, "Check for Updates...").trigger()
        self.assertTrue(
            self._wait_until(lambda: "text" in found),
            "no failure dialog appeared within 15 s")
        watchdog.stop()
        self.assertTrue(found.get("closed"), "the dialog had no OK button")
        self.assertEqual(QMessageBox.Icon.Warning, found["icon"])
        self.assertIn("Could not check for updates.", found["text"])
        self.assertIn("Could not reach GitHub: test failure", found["text"])

    def test_manual_check_while_running_is_refused(self):
        # A slow first check, then a second manual check while it is
        # still running. The second call must NOT run from inside the
        # watchdog: it blocks in a modal exec(), and a QTimer does not
        # re-fire while its own timeout handler is on the stack, so the
        # watchdog would never get to dismiss the refusal dialog. A
        # singleShot fires from the main loop instead, watchdog free.
        window = self._show_window()
        calls = {"n": 0}

        def slow(_version):
            calls["n"] += 1
            time.sleep(2)
            return dict(FAKE_CURRENT)

        self._patch_check(slow)
        texts: list = []

        def handle(dialog):
            if isinstance(dialog, QMessageBox):
                texts.append(dialog.text())
                if not _click_button(dialog, "OK"):
                    dialog.accept()

        watchdog = _DialogWatchdog(handle)
        watchdog.start()
        self.addCleanup(watchdog.stop)
        self._help_action(window, "Check for Updates...").trigger()
        QTimer.singleShot(200, window._on_manual_update_check)
        self.assertTrue(
            self._wait_until(
                lambda: "Already checking for updates." in texts),
            "no Already-checking refusal appeared within 15 s")
        self.assertTrue(
            self._wait_until(lambda: len(texts) >= 2),
            "the slow first check never reported its result")
        watchdog.stop()
        self.assertEqual(1, calls["n"],
                         "the second check ran a second update check")
        self.assertIn("Already checking for updates.", texts[0])
        self.assertTrue(
            any("You have the latest version." in t for t in texts),
            f"the slow check's result was not reported: {texts}")

    def test_help_menu_startup_action_toggles_and_persists(self):
        from android_photo_transfer.core.settings import SettingsManager

        window, _vm = self._make_window()
        action = self._help_action(window, "Check for updates on startup")
        self.assertTrue(action.isCheckable())
        self.assertTrue(action.isChecked(),
                        "the startup check is on by default")
        settings_manager = window._settings_manager
        action.trigger()  # on -> off
        self.assertFalse(settings_manager.current.check_updates_on_startup)
        reloaded = SettingsManager(settings_manager.path)
        self.assertFalse(reloaded.current.check_updates_on_startup,
                         "the toggled setting was not saved to disk")
        action.trigger()  # off -> on
        self.assertTrue(settings_manager.current.check_updates_on_startup)
        reloaded = SettingsManager(settings_manager.path)
        self.assertTrue(reloaded.current.check_updates_on_startup,
                        "the toggled setting was not saved to disk")


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
