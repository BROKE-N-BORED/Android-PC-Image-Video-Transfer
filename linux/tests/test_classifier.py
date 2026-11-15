"""Classifier tests. Mirrors upstream CoreTests.cs (FileCategoryTests) and
MediaClassifier.cs exactly.

CONTRACT (core/classifier.py) says PHOTO_EXTS has 20 entries and VIDEO_EXTS
13, "match MediaClassifier.cs exactly". The upstream file actually lists
20 photo extensions and 12 video extensions (.mp4 .mov .m4v .3gp .3g2
.mkv .avi .webm .wmv .mts .m2ts .ts) -- there is no 13th. These tests
assert the upstream sets verbatim; the CONTRACT's "13" looks like a
miscount and the integrator should fix the CONTRACT, not the code.

file_category() returns the CONTRACT's plural strings ("Photos",
"Videos", "Documents", "Audio", "Archives", "Apps", "Other"), not the
upstream enum names. folder_category() returns "Camera" | "Screenshots" |
"Downloads" | "Pictures" | "Other".
"""
from __future__ import annotations

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import try_import  # noqa: E402

classifier = try_import("android_photo_transfer.core.classifier")

# Verbatim from upstream MediaClassifier.cs.
UPSTREAM_PHOTO_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".webp", ".gif",
    ".bmp", ".tif", ".tiff", ".avif", ".dng", ".raw", ".arw", ".cr2",
    ".cr3", ".nef", ".orf", ".rw2",
}
UPSTREAM_VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mkv", ".avi", ".webm",
    ".wmv", ".mts", ".m2ts", ".ts",
}


class ClassifierTests(unittest.TestCase):

    def setUp(self):
        if classifier is None:
            self.skipTest("android_photo_transfer.core.classifier not built yet")

    def test_photo_extensions_match_upstream_exactly(self):
        self.assertEqual(UPSTREAM_PHOTO_EXTS, set(classifier.PHOTO_EXTS))
        self.assertEqual(20, len(classifier.PHOTO_EXTS))

    def test_video_extensions_match_upstream_exactly(self):
        self.assertEqual(UPSTREAM_VIDEO_EXTS, set(classifier.VIDEO_EXTS))
        self.assertEqual(12, len(classifier.VIDEO_EXTS),
                         "upstream MediaClassifier.cs lists 12, not the 13 "
                         "the CONTRACT claims; see module docstring")

    def test_jxl_is_not_a_photo(self):
        # CONTRACT explicitly excludes .jxl.
        self.assertFalse(classifier.is_photo("a.jxl"))
        self.assertNotIn(".jxl", set(classifier.PHOTO_EXTS))

    def test_is_photo_and_is_video_are_case_insensitive(self):
        self.assertTrue(classifier.is_photo("IMG_1.JPG"))
        self.assertTrue(classifier.is_photo("pic.Jpeg"))
        self.assertTrue(classifier.is_video("VID_1.MP4"))
        self.assertTrue(classifier.is_video("clip.MKV"))
        self.assertFalse(classifier.is_photo("clip.mp4"))
        self.assertFalse(classifier.is_video("pic.jpg"))
        self.assertFalse(classifier.is_photo("notes.txt"))
        self.assertFalse(classifier.is_video("notes.txt"))

    def test_raw_camera_formats_are_photos(self):
        for name in ("a.dng", "a.raw", "a.arw", "a.cr2", "a.cr3", "a.nef",
                     "a.orf", "a.rw2", "a.heic", "a.heif", "a.avif"):
            self.assertTrue(classifier.is_photo(name), name)

    def test_file_category(self):
        cases = [
            ("a.jpg", "Photos"),
            ("b.MP4", "Videos"),
            ("c.pdf", "Documents"),
            ("d.mp3", "Audio"),
            ("e.zip", "Archives"),
            ("f.apk", "Apps"),
            ("g.xyz", "Other"),
            ("noextension", "Other"),
            ("h.docx", "Documents"),
            ("i.flac", "Audio"),
            ("j.7z", "Archives"),
            ("k.xapk", "Apps"),
            ("l.tiff", "Photos"),
            ("m.m2ts", "Videos"),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, classifier.file_category(name))

    def test_folder_category(self):
        cases = [
            ("DCIM/Camera", "Camera"),
            ("DCIM", "Camera"),
            ("dcim/camera", "Camera"),
            ("Pictures/Screenshots", "Screenshots"),
            ("My Screenshots", "Screenshots"),
            ("Download", "Downloads"),
            ("Downloads/stuff", "Downloads"),
            ("download", "Downloads"),
            ("Pictures", "Pictures"),
            ("Pictures/Wallpapers", "Pictures"),
            ("Android/data/com.app/cache", "Other"),
            ("Movies", "Other"),
            ("", "Other"),
        ]
        for folder, expected in cases:
            with self.subTest(folder=folder):
                self.assertEqual(expected, classifier.folder_category(folder))

    def test_screenshots_win_over_camera_and_pictures(self):
        # Upstream checks "screenshot" before DCIM/Pictures.
        self.assertEqual("Screenshots", classifier.folder_category("DCIM/Screenshots"))
        self.assertEqual("Screenshots",
                         classifier.folder_category("Pictures/Screenshots"))


if __name__ == "__main__":
    unittest.main()
