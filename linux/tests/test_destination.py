"""Destination resolution tests. Mirrors upstream DestinationResolver +
FileUtilities behaviour (CoreTests.cs UtilityTests).

CONTRACT (core/destination.py):
  - resolve(dest_root, item, organize_by_folder) -> str: never-overwrite
    naming (name_1.jpg, name_2.jpg, ...); if organize_by_folder, mirror
    the phone folder under dest_root (sanitize path segments, block ".."
    escapes).
  - is_already_there(path, size) -> bool: same name + same size (name+size
    dedupe, matching upstream; NO hash in this build).

Filename sanitizing: upstream FileUtilities.SanitizeFileName replaces
Windows-invalid chars with "_" and maps blank names to "unnamed". On Linux
only "/" and NUL are truly illegal, so the port may sanitize less. The
sanitize tests below assert the hard guarantees through resolve() (no
subdirectory creation, no escape) and assert the upstream mapping only
when the module exposes a sanitize helper.
"""
from __future__ import annotations

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import TempFolder, make_media_item, try_import  # noqa: E402

dest_mod = try_import("android_photo_transfer.core.destination")
resolve = getattr(dest_mod, "resolve", None) if dest_mod else None
is_already_there = getattr(dest_mod, "is_already_there", None) if dest_mod else None


def _sanitize_fn():
    """A module-level sanitize helper, if the port exposes one."""
    if dest_mod is None:
        return None, None
    for name in ("sanitize_filename", "sanitize_file_name", "sanitize_name"):
        if hasattr(dest_mod, name):
            return getattr(dest_mod, name), name
    return None, None


class DestinationTests(unittest.TestCase):

    def setUp(self):
        if dest_mod is None or resolve is None:
            self.skipTest("android_photo_transfer.core.destination not built yet")
        self._folders: list[TempFolder] = []

    def tearDown(self):
        for folder in self._folders:
            folder.cleanup()

    def _dest(self) -> str:
        folder = TempFolder()
        self._folders.append(folder)
        return folder.path

    def test_plain_resolve_is_dest_root_plus_name(self):
        dest = self._dest()
        item = make_media_item("IMG_0001.jpg", 100)
        self.assertEqual(os.path.join(dest, "IMG_0001.jpg"),
                         resolve(dest, item, False))

    def test_never_overwrites_a_different_file(self):
        dest = self._dest()
        item = make_media_item("IMG_2.jpg", 800)
        with open(os.path.join(dest, "IMG_2.jpg"), "wb") as fh:
            fh.write(b"x" * 123)  # different size, same name
        self.assertEqual(os.path.join(dest, "IMG_2_1.jpg"),
                         resolve(dest, item, False))

    def test_numbering_keeps_going(self):
        dest = self._dest()
        item = make_media_item("IMG.jpg", 50)
        for name in ("IMG.jpg", "IMG_1.jpg", "IMG_2.jpg"):
            with open(os.path.join(dest, name), "wb") as fh:
                fh.write(b"x" * 999)
        self.assertEqual(os.path.join(dest, "IMG_3.jpg"),
                         resolve(dest, item, False))

    def test_identical_name_and_size_is_recognisable(self):
        # CONTRACT's resolve() takes no skip flag, so an identical file may
        # either resolve to the existing path (dedupe-aware) or to a
        # numbered sibling (dedupe left to is_already_there + the caller).
        # Either is acceptable; what must hold is that the caller can tell
        # it is a duplicate via is_already_there().
        dest = self._dest()
        item = make_media_item("IMG_1.jpg", 500)
        with open(os.path.join(dest, "IMG_1.jpg"), "wb") as fh:
            fh.write(b"x" * 500)
        resolved = resolve(dest, item, False)
        self.assertIn(resolved, (os.path.join(dest, "IMG_1.jpg"),
                                 os.path.join(dest, "IMG_1_1.jpg")))
        if is_already_there is not None:
            self.assertTrue(is_already_there(os.path.join(dest, "IMG_1.jpg"), 500))

    def test_organize_by_folder_mirrors_phone_folder(self):
        dest = self._dest()
        item = make_media_item("IMG_1.jpg", 100, folder="DCIM/Camera")
        self.assertEqual(os.path.join(dest, "DCIM", "Camera", "IMG_1.jpg"),
                         resolve(dest, item, True))

    def test_organize_by_folder_with_root_file_has_no_subfolder(self):
        dest = self._dest()
        item = make_media_item("loose.jpg", 100, folder="")
        self.assertEqual(os.path.join(dest, "loose.jpg"),
                         resolve(dest, item, True))

    def test_dotdot_folders_cannot_escape_the_destination(self):
        dest = self._dest()
        for folder in ("../DCIM/./Camera", "../../etc", "..", "DCIM/../../etc",
                       "/etc", "DCIM/Camera/../../../.."):
            with self.subTest(folder=folder):
                item = make_media_item("IMG.jpg", 100, folder=folder)
                resolved = resolve(dest, item, True)
                self.assertEqual(dest, os.path.commonpath([dest, resolved]),
                                 f"folder {folder!r} escaped to {resolved}")

    def test_slash_in_file_name_cannot_create_subfolders(self):
        dest = self._dest()
        item = make_media_item("sub/IMG.jpg", 100)
        resolved = resolve(dest, item, False)
        self.assertEqual(dest, os.path.dirname(resolved))
        self.assertNotIn("/", os.path.basename(resolved))

    def test_is_already_there_matches_name_and_size_only(self):
        if is_already_there is None:
            self.skipTest("is_already_there not defined in core.destination")
        dest = self._dest()
        path = os.path.join(dest, "IMG.jpg")
        with open(path, "wb") as fh:
            fh.write(b"x" * 500)
        self.assertTrue(is_already_there(path, 500))
        self.assertFalse(is_already_there(path, 501))
        self.assertFalse(is_already_there(os.path.join(dest, "missing.jpg"), 500))

    def test_sanitize_helper_matches_upstream_when_present(self):
        fn, name = _sanitize_fn()
        if fn is None:
            self.skipTest("core.destination exposes no sanitize helper; "
                          "sanitizing is only checked through resolve()")
        cases = [
            ("IMG:001?.jpg", "IMG_001_.jpg"),
            ("normal.jpg", "normal.jpg"),
            ("   ", "unnamed"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(expected, fn(raw))

    def test_sanitize_relative_folder_when_present(self):
        if dest_mod is None or not hasattr(dest_mod, "sanitize_relative_folder"):
            self.skipTest("core.destination exposes no sanitize_relative_folder")
        fn = dest_mod.sanitize_relative_folder
        self.assertEqual(os.path.join("DCIM", "Camera"),
                         fn("../DCIM/./Camera"))
        self.assertEqual("", fn("../.."))
        self.assertEqual(os.path.join("a", "b"), fn("a/b"))


if __name__ == "__main__":
    unittest.main()
