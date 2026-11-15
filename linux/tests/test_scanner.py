"""Scanner + MediaQuery tests. Mirrors upstream CoreTests.cs
(MediaScannerTests, MediaQueryTests).

CONTRACT (core/scanner.py):
  - scan(adapter, device_key, on_batch, should_stop): breadth-first walk of
    every storage root; priority folders first (DCIM, Pictures, Download,
    Movies, Documents); skip hidden/dot-folders ONLY (Android/data and
    Android/obb are INCLUDED); unreadable folders logged + skipped, never
    fatal; results streamed in batches via on_batch.
  - MediaItem: source_path, name, size, modified, created, folder (relative
    to root, "/"), folder_category, kind ("photo"|"video"|"file"),
    file_category, persistent_id, device_key, already_transferred.
  - MediaQuery.apply(items, show, search, sort_key, sort_desc):
    show in {"all", "not_on_pc", "on_pc"}; sort_key in
    {"name", "size", "date", "folder"}.
"""
from __future__ import annotations

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import FakeAdapter, TestData, make_media_item, try_import  # noqa: E402

scanner_mod = try_import("android_photo_transfer.core.scanner")

if scanner_mod is not None:
    scan = getattr(scanner_mod, "scan", None)
    MediaQuery = getattr(scanner_mod, "MediaQuery", None)
    MediaItem = getattr(scanner_mod, "MediaItem", None)
else:
    scan = MediaQuery = MediaItem = None


class ScannerTests(unittest.TestCase):

    def setUp(self):
        if scanner_mod is None or scan is None:
            self.skipTest("android_photo_transfer.core.scanner not built yet")

    def _scan(self, phone, device_key="fake-device-id"):
        found: list = []
        scan(phone, device_key, found.extend, lambda: False)
        return found

    def test_finds_photos_and_videos_and_android_data_and_skips_hidden_folders(self):
        phone = FakeAdapter()
        phone.add_file("DCIM/Camera/IMG_1.jpg", TestData.bytes(10))
        phone.add_file("DCIM/Camera/VID_1.MP4", TestData.bytes(10))
        phone.add_file("DCIM/.thumbnails/123.jpg", TestData.bytes(10))
        phone.add_file("Pictures/Screenshots/Screenshot_1.png", TestData.bytes(10))
        phone.add_file("Download/report.pdf", TestData.bytes(10))
        phone.add_file("Download/meme.webp", TestData.bytes(10))
        phone.add_file("Android/data/com.app/cache/x.jpg", TestData.bytes(10))
        phone.add_file("Android/media/com.whatsapp/WhatsApp/Media/IMG-WA0001.jpg",
                       TestData.bytes(10))

        found = self._scan(phone)

        names = sorted(f.name for f in found)
        # Every visible file is found (the Files tab needs them), including
        # Android data folders; hidden folders are skipped.
        self.assertEqual(["IMG-WA0001.jpg", "IMG_1.jpg", "Screenshot_1.png",
                          "VID_1.MP4", "meme.webp", "report.pdf", "x.jpg"], names)
        by_name = {f.name: f for f in found}
        self.assertEqual("file", by_name["report.pdf"].kind)
        self.assertEqual("Documents", by_name["report.pdf"].file_category)
        for item in found:
            self.assertIsNotNone(item.persistent_id, item.name)
        self.assertEqual("Camera", by_name["IMG_1.jpg"].folder_category)
        self.assertEqual("video", by_name["VID_1.MP4"].kind)
        self.assertEqual("Screenshots", by_name["Screenshot_1.png"].folder_category)
        self.assertEqual("Downloads", by_name["meme.webp"].folder_category)
        self.assertEqual("Other", by_name["IMG-WA0001.jpg"].folder_category)
        self.assertEqual("DCIM/Camera", by_name["IMG_1.jpg"].folder)
        self.assertEqual("Android/data/com.app/cache", by_name["x.jpg"].folder)

    def test_android_data_and_obb_are_included(self):
        phone = FakeAdapter()
        phone.add_file("Android/data/com.example/files/keep.jpg", TestData.bytes(10))
        phone.add_file("Android/obb/com.example/game.obb", TestData.bytes(10))
        found = self._scan(phone)
        self.assertEqual({"keep.jpg", "game.obb"}, {f.name for f in found})

    def test_hidden_files_and_hidden_folders_are_skipped(self):
        phone = FakeAdapter()
        phone.add_folder("DCIM/.hidden", hidden=True)
        phone.add_file("DCIM/.hidden/secret.jpg", TestData.bytes(10))
        phone.add_file("DCIM/Camera/.nomedia", TestData.bytes(10))
        phone.add_file("DCIM/Camera/visible.jpg", TestData.bytes(10))
        found = self._scan(phone)
        self.assertEqual(["visible.jpg"], [f.name for f in found])

    def test_priority_folders_come_first(self):
        # Same depth for every file: the scan is breadth-first, so a file
        # nested under DCIM/Camera legitimately arrives after a file
        # sitting directly in a lower-priority root folder. Priority
        # ordering applies between folders at the same level.
        phone = FakeAdapter()
        phone.add_file("Zebra/late.jpg", TestData.bytes(10))
        phone.add_file("Movies/clip.mp4", TestData.bytes(10))
        phone.add_file("DCIM/first.jpg", TestData.bytes(10))
        phone.add_file("Pictures/pic.png", TestData.bytes(10))
        phone.add_file("Android/last.jpg", TestData.bytes(10))
        found = self._scan(phone)
        order = [f.name for f in found]
        self.assertEqual(["first.jpg", "pic.png", "clip.mp4", "late.jpg",
                          "last.jpg"], order)

    def test_results_stream_in_batches(self):
        phone = FakeAdapter()
        for i in range(250):
            phone.add_file(f"DCIM/Camera/IMG_{i:03d}.jpg", TestData.bytes(10))
        batches: list[list] = []
        scan(phone, "fake-device-id", batches.append, lambda: False)
        total = sum(len(b) for b in batches)
        self.assertEqual(250, total)
        self.assertGreater(len(batches), 1, "250 files must arrive in more than one batch")

    def test_should_stop_halts_the_scan(self):
        # Files spread over several folders: should_stop is checked
        # between folders, so a single-folder scan cannot stop early
        # (one batch already holds everything).
        phone = FakeAdapter()
        for folder in range(10):
            for i in range(5):
                phone.add_file(f"DCIM/Set{folder}/IMG_{i}.jpg", TestData.bytes(10))
        found: list = []
        calls = {"n": 0}

        def stop_after_first_batch(batch):
            found.extend(batch)
            calls["n"] += 1

        scan(phone, "fake-device-id", stop_after_first_batch,
             lambda: calls["n"] >= 1)
        self.assertLess(len(found), 50, "scan kept going after should_stop turned true")
        self.assertGreater(len(found), 0, "scan stopped before finding anything")

    def test_unreadable_folder_is_skipped_not_fatal(self):
        phone = FakeAdapter()
        phone.add_file("DCIM/Camera/good.jpg", TestData.bytes(10))
        phone.add_file("Broken/inside.jpg", TestData.bytes(10))
        real_list = phone.list_folder

        def flaky_list(path, on_batch):
            if path.endswith("/Broken"):
                raise OSError("simulated unreadable folder")
            return real_list(path, on_batch)

        phone.list_folder = flaky_list  # type: ignore[method-assign]
        found = self._scan(phone)
        self.assertEqual(["good.jpg"], [f.name for f in found])

    def test_device_key_is_stamped_on_every_item(self):
        phone = FakeAdapter()
        phone.add_file("DCIM/a.jpg", TestData.bytes(10))
        found = self._scan(phone, device_key="SN:PHONE-2")
        self.assertEqual(["SN:PHONE-2"], [f.device_key for f in found])
        self.assertFalse(found[0].already_transferred)


class MediaQueryTests(unittest.TestCase):

    def setUp(self):
        if scanner_mod is None or MediaQuery is None:
            self.skipTest("MediaQuery not built yet (core/scanner.py)")

    def _items(self):
        return [
            make_media_item("b.jpg", 300, day=2),
            make_media_item("a.jpg", 100, day=3),
            make_media_item("c.mp4", 200, day=1),
            make_media_item("Screenshot_1.png", 50, day=4, folder="Pictures/Screenshots"),
        ]

    def _apply(self, items, **kwargs):
        params = {"show": "all", "search": "", "sort_key": "date", "sort_desc": True}
        params.update(kwargs)
        return MediaQuery.apply(items, **params)

    def test_sorts_by_date_newest_first(self):
        names = [i.name for i in self._apply(self._items())]
        self.assertEqual(["Screenshot_1.png", "a.jpg", "b.jpg", "c.mp4"], names)

    def test_sorts_by_date_oldest_first(self):
        names = [i.name for i in self._apply(self._items(), sort_desc=False)]
        self.assertEqual(["c.mp4", "b.jpg", "a.jpg", "Screenshot_1.png"], names)

    def test_sorts_by_size_largest_first(self):
        names = [i.name for i in self._apply(self._items(), sort_key="size", sort_desc=True)]
        self.assertEqual(["b.jpg", "c.mp4", "a.jpg", "Screenshot_1.png"], names)

    def test_sorts_by_name(self):
        names = [i.name for i in self._apply(self._items(), sort_key="name", sort_desc=False)]
        self.assertEqual(["a.jpg", "b.jpg", "c.mp4", "Screenshot_1.png"], names)

    def test_search_matches_name_folder_and_extension(self):
        items = self._items()
        self.assertEqual(["Screenshot_1.png"],
                         [i.name for i in self._apply(items, search="screenshots",
                                                       sort_key="name", sort_desc=False)])
        self.assertEqual(["a.jpg", "b.jpg"],
                         [i.name for i in self._apply(items, search=".jpg",
                                                       sort_key="name", sort_desc=False)])
        self.assertEqual(["c.mp4"],
                         [i.name for i in self._apply(items, search="c.mp4",
                                                       sort_key="name", sort_desc=False)])

    def test_show_filters_on_already_transferred(self):
        items = self._items()
        items[0].already_transferred = True  # b.jpg
        items[2].already_transferred = True  # c.mp4
        self.assertEqual({"a.jpg", "Screenshot_1.png"},
                         {i.name for i in self._apply(items, show="not_on_pc")})
        self.assertEqual({"b.jpg", "c.mp4"},
                         {i.name for i in self._apply(items, show="on_pc")})
        self.assertEqual(4, len(self._apply(items, show="all")))


if __name__ == "__main__":
    unittest.main()
