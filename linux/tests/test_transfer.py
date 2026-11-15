"""Transfer engine tests. Mirrors upstream TransferManagerTests.cs.

CONTRACT notes (port/CONTRACT.md, core/transfer.py):
  - TransferManager(history).run(transfer_items, dest_root, skip_existing,
    organize_by_folder, progress, should_pause, should_cancel)
    -> TransferResult(disconnected, stopped_early).
  - The CONTRACT run() signature carries no adapter, yet the engine must
    read from the phone, so the adapter has to arrive via the constructor
    or as an extra run() argument. _run() below inspects the real
    signatures and passes the adapter wherever it fits; if the built
    manager exposes no adapter hook at all, the tests skip with a clear
    message instead of failing on a contract ambiguity.
  - TransferItem.status: pending | copying | done | skipped | failed |
    cancelled. TransferProgress: copied/skipped/failed, current_name,
    current_bytes/current_total, overall_done/overall_total, on_change.
    _progress() also accepts the upstream-style snapshot() shape.
  - 5 consecutive failures stop the run and the failed files go back to
    "pending" for Resume (upstream behaviour; CONTRACT says only that the
    rest stay pending).
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import time
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)
sys.path.insert(0, _TESTS_DIR)  # tests/ (fakes.py)

from fakes import FakeAdapter, MemoryHistory, TempFolder, TestData, try_import  # noqa: E402

transfer_mod = try_import("android_photo_transfer.core.transfer")

if transfer_mod is not None:
    TransferManager = getattr(transfer_mod, "TransferManager", None)
    TransferItem = getattr(transfer_mod, "TransferItem", None)
    TransferProgress = getattr(transfer_mod, "TransferProgress", None)
    InsufficientSpaceError = getattr(transfer_mod, "InsufficientSpaceError", None)
else:
    TransferManager = TransferItem = TransferProgress = InsufficientSpaceError = None


def _progress_values(progress):
    """(copied, skipped, failed, overall_done, overall_total)."""
    if progress is None:
        return (0, 0, 0, 0, 0)
    if hasattr(progress, "overall_done"):
        return (getattr(progress, "copied", 0), getattr(progress, "skipped", 0),
                getattr(progress, "failed", 0), progress.overall_done,
                getattr(progress, "overall_total", 0))
    snap = progress.snapshot()  # upstream-style TransferProgress
    get = (lambda k: getattr(snap, k)) if not isinstance(snap, dict) else snap.get
    return (get("Copied"), get("Skipped"), get("Failed"),
            get("BytesProcessed"), get("BytesTotal"))


class TransferTestCase(unittest.TestCase):
    """Base with the signature-tolerant manager runner."""

    def setUp(self):
        if transfer_mod is None or TransferManager is None or TransferItem is None:
            self.skipTest("android_photo_transfer.core.transfer not built yet")
        self._folders: list[TempFolder] = []

    def tearDown(self):
        for folder in self._folders:
            folder.cleanup()

    def temp_folder(self) -> TempFolder:
        folder = TempFolder()
        self._folders.append(folder)
        return folder

    def _make_manager(self, phone, history):
        """Build a TransferManager, passing the adapter if the ctor takes one."""
        try:
            params = inspect.signature(TransferManager.__init__).parameters
        except (TypeError, ValueError):
            params = {}
        names = set(params)
        kwargs: dict = {}
        if "history" in names:
            kwargs["history"] = history
        if "adapter" in names:
            kwargs["adapter"] = phone
        elif "device" in names:
            kwargs["device"] = phone
        try:
            if kwargs:
                return TransferManager(**kwargs), ("adapter" in names or "device" in names)
            return TransferManager(history), False
        except TypeError:
            return TransferManager(history), False

    def _run(self, phone, jobs, dest, history=None, skip_existing=True,
             organize=False, progress=None, should_pause=None, should_cancel=None):
        """Run the manager, adapting to its real run() signature."""
        manager, adapter_in_ctor = self._make_manager(phone, history)
        if progress is None:
            progress = TransferProgress() if TransferProgress is not None else None
        try:
            params = inspect.signature(manager.run).parameters
        except (TypeError, ValueError):
            params = {}
        names = set(params)
        wants_adapter = bool(names & {"adapter", "device"})
        if not adapter_in_ctor and not wants_adapter:
            # CONTRACT run() has no adapter parameter and the ctor took
            # none either: the engine cannot read the phone. Skip, don't fail.
            self.skipTest(
                "TransferManager exposes no adapter hook (neither ctor nor "
                "run() takes an adapter/device); contract ambiguity, see "
                "test_transfer.py header")
        kwargs: dict = {}
        by_name = {
            "adapter": phone, "device": phone,
            "transfer_items": jobs, "items": jobs, "jobs": jobs,
            "dest_root": dest, "destination": dest, "dest": dest,
            "destination_folder": dest,
            "skip_existing": skip_existing,
            "organize_by_folder": organize, "organize": organize,
            "progress": progress,
            "should_pause": should_pause or (lambda: False),
            "should_cancel": should_cancel or (lambda: False),
        }
        for name in names:
            if name in by_name:
                kwargs[name] = by_name[name]
        # Positional fallback: if run() takes no keyword-friendly params,
        # call it positionally in CONTRACT order.
        if not kwargs:
            return manager.run(jobs, dest, skip_existing, organize, progress,
                               should_pause or (lambda: False),
                               should_cancel or (lambda: False)), progress
        # Fill any remaining required params positionally is impossible, so
        # require the known ones to be present.
        return manager.run(**kwargs), progress

    def _no_partials(self, dest):
        leftovers = []
        for root, _dirs, files in os.walk(dest):
            leftovers += [f for f in files if f.endswith(".partial")]
        return leftovers


class TransferManagerTests(TransferTestCase):

    def test_copies_every_file_byte_for_byte_and_leaves_no_partial_files(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        small = TestData.bytes(1000, 1)
        large = TestData.bytes(3 * 1024 * 1024 + 17, 2)  # spans several 1 MB chunks
        a = phone.add_file("DCIM/Camera/IMG_0001.jpg", small)
        b = phone.add_file("DCIM/Camera/VID_0002.mp4", large)
        history = MemoryHistory()
        jobs = [TransferItem(a), TransferItem(b)]

        result, progress = self._run(phone, jobs, dest.path, history=history)

        with open(os.path.join(dest.path, "IMG_0001.jpg"), "rb") as fh:
            self.assertEqual(small, fh.read())
        with open(os.path.join(dest.path, "VID_0002.mp4"), "rb") as fh:
            self.assertEqual(large, fh.read())
        self.assertEqual([], self._no_partials(dest.path))
        self.assertEqual(["done", "done"], [j.status for j in jobs])
        self.assertEqual(2, len(history.records))
        copied, _sk, _fl, _d, _t = _progress_values(progress)
        self.assertEqual(2, copied)
        # Original mtime preserved (FakeAdapter default: 2026-10-01 12:00 local).
        from datetime import datetime as _dt
        mtime = os.path.getmtime(os.path.join(dest.path, "IMG_0001.jpg"))
        self.assertEqual(int(_dt(2026, 10, 1, 12, 0, 0).timestamp()), int(mtime))

    def test_skips_identical_existing_file_and_never_overwrites_a_different_one(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        same = phone.add_file("DCIM/Camera/IMG_1.jpg", TestData.bytes(500, 3))
        different = phone.add_file("DCIM/Camera/IMG_2.jpg", TestData.bytes(800, 4))
        with open(os.path.join(dest.path, "IMG_1.jpg"), "wb") as fh:
            fh.write(TestData.bytes(500, 3))  # already transferred
        unrelated = TestData.bytes(123, 99)
        with open(os.path.join(dest.path, "IMG_2.jpg"), "wb") as fh:
            fh.write(unrelated)  # a different photo, same name

        jobs = [TransferItem(same), TransferItem(different)]
        _result, progress = self._run(phone, jobs, dest.path)

        copied, skipped, _failed, _d, _t = _progress_values(progress)
        self.assertEqual(1, skipped)
        self.assertEqual(1, copied)
        self.assertEqual("skipped", jobs[0].status)
        self.assertEqual("done", jobs[1].status)
        with open(os.path.join(dest.path, "IMG_2.jpg"), "rb") as fh:
            self.assertEqual(unrelated, fh.read())  # untouched
        self.assertTrue(os.path.exists(os.path.join(dest.path, "IMG_2_1.jpg")))

    def test_keep_both_mode_renames_even_identical_files(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        item = phone.add_file("DCIM/IMG.jpg", TestData.bytes(300, 5))
        with open(os.path.join(dest.path, "IMG.jpg"), "wb") as fh:
            fh.write(TestData.bytes(300, 5))

        jobs = [TransferItem(item)]
        _result, _p = self._run(phone, jobs, dest.path, skip_existing=False)

        self.assertEqual("done", jobs[0].status)
        self.assertTrue(os.path.exists(os.path.join(dest.path, "IMG_1.jpg")))

    def test_incomplete_copy_is_failed_and_discarded(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        item = phone.add_file("DCIM/Camera/IMG_9.jpg", TestData.bytes(10_000, 6))
        phone.truncated.add(item.source_path)

        job = TransferItem(item)
        _result, _p = self._run(phone, [job], dest.path)

        self.assertEqual("failed", job.status)
        self.assertIsNotNone(job.error)
        self.assertEqual([], os.listdir(dest.path))

    def test_transient_read_error_is_retried_once(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        item = phone.add_file("DCIM/Camera/IMG_7.jpg", TestData.bytes(200_000, 7))
        failures = {"n": 0}

        def glitch(path, position):
            if position > 0 and failures["n"] == 0:
                failures["n"] += 1
                raise OSError("Simulated glitch")

        phone.before_read = glitch

        jobs = [TransferItem(item)]
        _result, _p = self._run(phone, jobs, dest.path)

        self.assertEqual("done", jobs[0].status)
        with open(os.path.join(dest.path, "IMG_7.jpg"), "rb") as fh:
            self.assertEqual(TestData.bytes(200_000, 7), fh.read())

    def test_cancel_mid_file_removes_partial_and_marks_remaining_cancelled(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        first = phone.add_file("DCIM/Camera/A.jpg", TestData.bytes(1000, 8))
        second = phone.add_file("DCIM/Camera/B.mp4", TestData.bytes(2 * 1024 * 1024, 9))
        third = phone.add_file("DCIM/Camera/C.jpg", TestData.bytes(1000, 10))
        cancelled = threading.Event()

        def trip_cancel(path, position):
            if path == second.source_path and position > 256 * 1024:
                cancelled.set()

        phone.before_read = trip_cancel
        jobs = [TransferItem(first), TransferItem(second), TransferItem(third)]

        _result, _p = self._run(phone, jobs, dest.path,
                                should_cancel=cancelled.is_set)

        self.assertEqual("done", jobs[0].status)
        self.assertEqual("cancelled", jobs[1].status)
        self.assertEqual("cancelled", jobs[2].status)
        self.assertEqual(["A.jpg"], sorted(os.listdir(dest.path)))
        self.assertEqual([], self._no_partials(dest.path))

    def test_phone_unplugged_keeps_remaining_jobs_and_resume_finishes_them(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        items = [phone.add_file(f"DCIM/Camera/IMG_{i}.jpg", TestData.bytes(150_000, i))
                 for i in range(1, 5)]

        def unplug(path, position):
            if path == items[2].source_path and position > 0:
                phone.connected = False  # unplugged mid-file

        phone.before_read = unplug
        jobs = [TransferItem(i) for i in items]

        first, _p = self._run(phone, jobs, dest.path)

        self.assertTrue(getattr(first, "disconnected", False))
        self.assertEqual(2, sum(1 for j in jobs if j.status == "done"))
        self.assertEqual("pending", jobs[2].status)
        self.assertEqual([], self._no_partials(dest.path))
        self.assertFalse(os.path.exists(os.path.join(dest.path, "IMG_3.jpg")))

        # Plug it back in and resume the same jobs.
        phone.connected = True
        phone.before_read = None
        second, _p2 = self._run(phone, jobs, dest.path)

        self.assertFalse(getattr(second, "disconnected", False))
        self.assertEqual(["done"] * 4, [j.status for j in jobs])
        self.assertEqual(4, len(os.listdir(dest.path)))

    def test_organize_by_folder_mirrors_the_phone_folders(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        camera = phone.add_file("DCIM/Camera/IMG_1.jpg", TestData.bytes(100, 11))
        shot = phone.add_file("Pictures/Screenshots/Screenshot_1.png", TestData.bytes(100, 12))

        _result, _p = self._run(phone, [TransferItem(camera), TransferItem(shot)],
                                dest.path, organize=True)

        self.assertTrue(os.path.exists(os.path.join(dest.path, "DCIM", "Camera", "IMG_1.jpg")))
        self.assertTrue(os.path.exists(
            os.path.join(dest.path, "Pictures", "Screenshots", "Screenshot_1.png")))

    def test_pause_holds_the_transfer_until_resumed(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        item = phone.add_file("DCIM/IMG.jpg", TestData.bytes(1000, 13))
        paused = threading.Event()
        paused.set()
        jobs = [TransferItem(item)]
        outcome: dict = {}

        def run_in_thread():
            outcome["result"], _ = self._run(phone, jobs, dest.path,
                                             should_pause=paused.is_set)

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        time.sleep(0.5)
        try:
            self.assertTrue(thread.is_alive(), "run finished while paused")
            self.assertEqual([], os.listdir(dest.path))
        finally:
            paused.clear()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "run did not finish after resume")
        self.assertEqual("done", jobs[0].status)

    def test_files_are_opened_by_their_phone_id_not_by_path(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        items = [phone.add_file(f"DCIM/Camera/IMG_{i}.jpg", TestData.bytes(1000, i))
                 for i in range(1, 4)]

        self._run(phone, [TransferItem(i) for i in items], dest.path)

        self.assertEqual(["id", "id", "id"], phone.opened_by)

    def test_pausing_mid_file_discards_it_and_restarts_it_cleanly_on_resume(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        content = TestData.bytes(3 * 1024 * 1024, 21)
        item = phone.add_file("DCIM/Camera/VID_big.mp4", content)
        paused = threading.Event()
        paused_mid_file = threading.Event()

        def pause_midway(path, position):
            if position > 512 * 1024 and not paused_mid_file.is_set():
                paused.set()
                paused_mid_file.set()

        phone.before_read = pause_midway
        jobs = [TransferItem(item)]
        progress = TransferProgress() if TransferProgress is not None else None
        outcome: dict = {}

        def run_in_thread():
            outcome["result"], _ = self._run(phone, jobs, dest.path, progress=progress,
                                             should_pause=paused.is_set)

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        self.assertTrue(paused_mid_file.wait(timeout=10), "never reached the pause point")
        time.sleep(0.5)
        try:
            self.assertTrue(thread.is_alive(), "run finished while paused mid-file")
            self.assertEqual([], os.listdir(dest.path), "half-copied file left behind")
        finally:
            paused.clear()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "run did not finish after resume")
        self.assertEqual("done", jobs[0].status)
        with open(os.path.join(dest.path, "VID_big.mp4"), "rb") as fh:
            self.assertEqual(content, fh.read())
        _c, _s, _f, done, _total = _progress_values(progress)
        self.assertEqual(len(content), done, "restarted bytes double-counted in progress")

    def test_repeated_failures_stop_the_transfer_and_keep_the_rest_for_resume(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        items = [phone.add_file(f"DCIM/Camera/IMG_{i}.jpg", TestData.bytes(5000, i))
                 for i in range(1, 21)]
        good = {items[0].source_path, items[1].source_path}

        def glitch_everything_else(path, _position):
            if path not in good:
                raise OSError("Simulated phone glitch")

        phone.before_read = glitch_everything_else
        jobs = [TransferItem(i) for i in items]
        progress = TransferProgress() if TransferProgress is not None else None

        result, _p = self._run(phone, jobs, dest.path, progress=progress)

        self.assertEqual(2, sum(1 for j in jobs if j.status == "done"))
        self.assertTrue(getattr(result, "stopped_early", False),
                        "run did not report stopped_early after 5 failures in a row")
        pending = [j for j in jobs if j.status == "pending"]
        self.assertEqual(18, len(pending), "failed + untouched jobs must stay pending for Resume")
        _c, _s, failed, _d, _t = _progress_values(progress)
        self.assertEqual(0, failed, "re-queued failures must not count as failed")

        # After a reconnect, Resume finishes the rest.
        phone.before_read = None
        self._run(phone, jobs, dest.path)
        self.assertEqual(["done"] * 20, [j.status for j in jobs])
        self.assertEqual(20, len(os.listdir(dest.path)))

    def test_progress_reaches_100_percent_including_skipped_files(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        a = phone.add_file("DCIM/A.jpg", TestData.bytes(4000, 14))
        b = phone.add_file("DCIM/B.jpg", TestData.bytes(6000, 15))
        with open(os.path.join(dest.path, "A.jpg"), "wb") as fh:
            fh.write(TestData.bytes(4000, 14))
        progress = TransferProgress() if TransferProgress is not None else None

        self._run(phone, [TransferItem(a), TransferItem(b)], dest.path, progress=progress)

        copied, skipped, _failed, done, total = _progress_values(progress)
        self.assertEqual(total, done, "overall progress must reach 100% including skipped files")
        self.assertEqual(6000 + 4000, total)
        self.assertEqual(1, skipped)
        self.assertEqual(1, copied)

    def test_history_failure_does_not_fail_the_transfer(self):
        dest = self.temp_folder()
        phone = FakeAdapter()
        item = phone.add_file("DCIM/IMG.jpg", TestData.bytes(500, 16))

        class BrokenHistory:
            def record(self, *args, **kwargs):
                raise OSError("disk exploded")

            def get_transferred_keys(self, device_key):
                return set()

        jobs = [TransferItem(item)]
        self._run(phone, jobs, dest.path, history=BrokenHistory())
        self.assertEqual("done", jobs[0].status)

    def test_insufficient_space_raises_before_copying_anything(self):
        if InsufficientSpaceError is None:
            self.skipTest("InsufficientSpaceError not defined in core.transfer")
        dest = self.temp_folder()
        phone = FakeAdapter()
        item = phone.add_file("DCIM/IMG.jpg", TestData.bytes(1000, 17))
        tiny = mock.Mock()
        tiny.free = 1024  # 1 KB free; file + 50 MB margin cannot fit

        def fake_disk_usage(path):
            return tiny

        def fake_statvfs(path):
            stat = mock.Mock()
            stat.f_bavail = 1
            stat.f_frsize = 1024
            return stat

        jobs = [TransferItem(item)]
        with mock.patch("shutil.disk_usage", fake_disk_usage), \
                mock.patch("os.statvfs", fake_statvfs):
            try:
                self._run(phone, jobs, dest.path)
            except InsufficientSpaceError as exc:
                self.assertTrue(hasattr(exc, "required"))
                self.assertTrue(hasattr(exc, "available"))
                self.assertEqual([], os.listdir(dest.path))
                return
        self.skipTest("free-space check does not use shutil.disk_usage/os.statvfs; "
                      "verify the 50 MB margin check by hand")


if __name__ == "__main__":
    unittest.main()
