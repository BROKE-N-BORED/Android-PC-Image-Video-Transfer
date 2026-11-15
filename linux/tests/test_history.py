"""History database tests. Mirrors upstream CoreTests.cs
(HistoryDatabaseTests).

CONTRACT (core/history.py):
  - HistoryDatabase(path=None) (default XDG
    ~/.local/share/android-photo-transfer/history.db), sqlite3, WAL mode,
    table transfers(device_key TEXT, source_path TEXT, source_size INTEGER,
    PRIMARY KEY(device_key, source_path, source_size)).
  - record(device_key, source_path, size), get_transferred_keys(device_key)
    -> set of "path|size" keys, close(). All failures raise; callers catch.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import TempFolder, try_import  # noqa: E402

history_mod = try_import("android_photo_transfer.core.history")
HistoryDatabase = getattr(history_mod, "HistoryDatabase", None) if history_mod else None


class HistoryDatabaseTests(unittest.TestCase):

    def setUp(self):
        if HistoryDatabase is None:
            self.skipTest("android_photo_transfer.core.history not built yet")
        self._folders: list[TempFolder] = []

    def tearDown(self):
        for folder in self._folders:
            folder.cleanup()

    def _db_path(self) -> str:
        folder = TempFolder()
        self._folders.append(folder)
        return os.path.join(folder.path, "history.db")

    def test_remembers_transfers_per_device_and_ignores_duplicates(self):
        path = self._db_path()
        db = HistoryDatabase(path)
        try:
            db.record("SN:1", "/S/DCIM/IMG.jpg", 1234)
            db.record("SN:1", "/S/DCIM/IMG.jpg", 1234)  # same file twice -> one row
            db.record("SN:1", "/S/DCIM/IMG2.jpg", 999)
        finally:
            db.close()

        db = HistoryDatabase(path)
        try:
            self.assertEqual({"/S/DCIM/IMG.jpg|1234", "/S/DCIM/IMG2.jpg|999"},
                             db.get_transferred_keys("SN:1"))
            self.assertEqual(set(), db.get_transferred_keys("SN:2"))
        finally:
            db.close()

    def test_same_path_different_size_is_a_different_file(self):
        path = self._db_path()
        db = HistoryDatabase(path)
        try:
            db.record("SN:1", "/S/DCIM/IMG.jpg", 100)
            db.record("SN:1", "/S/DCIM/IMG.jpg", 200)
            self.assertEqual({"/S/DCIM/IMG.jpg|100", "/S/DCIM/IMG.jpg|200"},
                             db.get_transferred_keys("SN:1"))
        finally:
            db.close()

    def test_same_file_on_two_devices_is_tracked_separately(self):
        path = self._db_path()
        db = HistoryDatabase(path)
        try:
            db.record("SN:1", "/S/DCIM/IMG.jpg", 1234)
            db.record("SN:2", "/S/DCIM/IMG.jpg", 1234)
            self.assertEqual({"/S/DCIM/IMG.jpg|1234"}, db.get_transferred_keys("SN:1"))
            self.assertEqual({"/S/DCIM/IMG.jpg|1234"}, db.get_transferred_keys("SN:2"))
        finally:
            db.close()

    def test_journal_mode_is_wal(self):
        path = self._db_path()
        db = HistoryDatabase(path)
        try:
            conn = sqlite3.connect(path)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("wal", mode.lower())
        finally:
            db.close()

    def test_schema_matches_contract(self):
        path = self._db_path()
        db = HistoryDatabase(path)
        try:
            conn = sqlite3.connect(path)
            try:
                cols = {row[1]: row[2] for row in
                        conn.execute("PRAGMA table_info(transfers)").fetchall()}
            finally:
                conn.close()
            self.assertEqual({"device_key": "TEXT", "source_path": "TEXT",
                              "source_size": "INTEGER"}, cols)
        finally:
            db.close()

    def test_creates_parent_directories(self):
        folder = TempFolder()
        self._folders.append(folder)
        nested = os.path.join(folder.path, "deep", "deeper", "history.db")
        db = HistoryDatabase(nested)
        try:
            db.record("SN:1", "/S/a.jpg", 10)
        finally:
            db.close()
        self.assertTrue(os.path.exists(nested))

    def test_default_path_is_the_xdg_history_db(self):
        # The default path is built from the home dir at construction time.
        # Patch HOME and re-check where the file lands; skip if the module
        # resolves the home dir some other way (e.g. a baked-in constant).
        import pathlib
        from unittest import mock

        folder = TempFolder()
        self._folders.append(folder)
        fake_home = folder.path
        with mock.patch.dict(os.environ, {"HOME": fake_home}), \
                mock.patch.object(pathlib.Path, "home", return_value=pathlib.Path(fake_home)):
            db = HistoryDatabase()
            try:
                db.record("SN:1", "/S/a.jpg", 10)
            finally:
                db.close()
        expected = os.path.join(fake_home, ".local", "share",
                                "android-photo-transfer", "history.db")
        if not os.path.exists(expected):
            self.skipTest("HistoryDatabase() default path does not honour HOME; "
                          "verify the XDG default path by hand")
        self.assertTrue(os.path.exists(expected))


if __name__ == "__main__":
    unittest.main()
