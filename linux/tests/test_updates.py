"""Auto-updater core tests (v0.1.1). Qt-free unittest: runs under plain
system python3, no PySide6, and NO real network anywhere -- every HTTP
call goes through a stub urlopen (any URL the stub does not know is a
test failure, never a network call), and every install is a temp dir.

CONTRACT (core/updates.py):
  - Versions: parse_version / compare_versions / is_newer. "v" prefix
    accepted, shorter tuples pad with zeros, an unparsable version is
    never reported as an update.
  - Release selection is by ASSET NAME, never by tag: a release only
    counts when it carries BOTH android-photo-transfer-linux-<ver>.zip
    AND its .zip.sha256 sidecar for the same version. Windows releases
    (no Linux zip), drafts and prereleases never count.
  - check_for_update -> {"state": "newer"|"current"|"unknown", ...};
    it never raises for network/parsing trouble ("unknown" carries a
    plain-language reason instead).
  - download_update streams the zip, SHA-256-checks it against the
    sidecar, and deletes the file on any failure. Cancel raises
    UpdateCancelled and removes the partial file.
  - apply_update replaces the install by atomic swap: extract to temp,
    check the extracted __version__ matches, build the new tree in a
    staging dir beside the install, move the keep-list (venv,
    history.db, history.db-wal, history.db-shm, logs) into it, rename
    the install dir to a backup, rename staging into place, delete the
    backup, verify the new tree imports. Any failure before the swap
    leaves the old install untouched.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # port/ (the package)

try:
    from android_photo_transfer.core import updates
    from android_photo_transfer.core.errors import describe
except Exception:  # pragma: no cover - module not built yet
    updates = None
    describe = None

ZIP_URL = "https://example.invalid/release.zip"
SIDECAR_URL = "https://example.invalid/release.zip.sha256"


# ---------------------------------------------------------------------------
# Helpers: stub HTTP, fake releases, fake zips and installs
# ---------------------------------------------------------------------------

class _FakeResponse(io.BytesIO):
    """A urlopen stub response: bytes in, Content-Length header optional."""

    def __init__(self, data, content_length=True):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))} if content_length else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _ChunkedResponse(_FakeResponse):
    """Returns at most `chunk` bytes per read(), so a download takes
    several loop iterations (needed to cancel mid-download)."""

    def __init__(self, data, chunk=10):
        super().__init__(data)
        self._chunk = chunk

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._chunk
        return super().read(min(size, self._chunk))


def _stub_urlopen(mapping):
    """mapping: exact URL -> bytes | _FakeResponse | Exception to raise.
    Any URL not in the map is a test bug, never a real network call."""
    def _open(url, timeout=None):
        if url not in mapping:
            raise AssertionError(f"stub urlopen got an unexpected URL: {url}")
        payload = mapping[url]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, _FakeResponse):
            return payload
        return _FakeResponse(payload)
    return _open


def _releases_urlopen(payload):
    """A urlopen stub answering any URL with the given releases payload
    (a list/dict, serialised as JSON, or raw bytes)."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return _stub_urlopen_for_any(data)


def _stub_urlopen_for_any(data):
    def _open(url, timeout=None):
        return _FakeResponse(data)
    return _open


def _linux_assets(version, base="https://example.invalid"):
    return [
        {"name": f"android-photo-transfer-linux-{version}.zip",
         "browser_download_url": f"{base}/{version}.zip"},
        {"name": f"android-photo-transfer-linux-{version}.zip.sha256",
         "browser_download_url": f"{base}/{version}.zip.sha256"},
    ]


def _release(tag, assets, draft=False, prerelease=False):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/x/releases/{tag}",
        "assets": assets,
    }


def _make_zip(path, version, extra=None):
    """A release zip: package with __version__, README, icons."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("android_photo_transfer/__init__.py",
                    f'__version__ = "{version}"\n')
        zf.writestr("android_photo_transfer/new.py", "NEW = True\n")
        zf.writestr("README.md", f"Android Photo Transfer {version}\n")
        zf.writestr("icons/icon.png", b"\x89PNG fake\n")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _make_install(root, version="0.1.1"):
    """A fake installed tree: launcher + package + the full keep-list
    (venv, history.db, history.db-wal, history.db-shm, logs). The venv
    deliberately has no bin/python: _verify_tree would otherwise try to
    run it, and the keep-list move is what is under test here."""
    install_dir = os.path.join(root, "install")
    pkg = os.path.join(install_dir, "android_photo_transfer")
    os.makedirs(pkg)
    with open(os.path.join(pkg, "__init__.py"), "w") as fh:
        fh.write(f'__version__ = "{version}"\n')
    with open(os.path.join(pkg, "old.py"), "w") as fh:
        fh.write("OLD = True\n")
    launcher = os.path.join(install_dir, "android-photo-transfer")
    with open(launcher, "w") as fh:
        fh.write("#!/bin/sh\nexec python3 -m android_photo_transfer \"$@\"\n")
    os.chmod(launcher, 0o755)
    for name, content in (("history.db", "HISTORY"),
                          ("history.db-wal", "WAL"),
                          ("history.db-shm", "SHM")):
        with open(os.path.join(install_dir, name), "w") as fh:
            fh.write(content)
    os.makedirs(os.path.join(install_dir, "logs"))
    with open(os.path.join(install_dir, "logs", "app.log"), "w") as fh:
        fh.write("LOGS")
    os.makedirs(os.path.join(install_dir, "venv", "bin"))
    with open(os.path.join(install_dir, "venv", "bin", "activate"), "w") as fh:
        fh.write("# fake venv\n")
    return install_dir


def _leftovers(parent):
    return [n for n in os.listdir(parent) if n.startswith(".apt-update-")]


class UpdatesTestCase(unittest.TestCase):
    def setUp(self):
        if updates is None:
            self.skipTest("android_photo_transfer.core.updates not built yet")
        self._tmp = tempfile.TemporaryDirectory(prefix="apt-updates-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

class VersionTests(UpdatesTestCase):

    def test_parse_plain_and_v_prefix(self):
        self.assertEqual((0, 1, 2), updates.parse_version("0.1.2"))
        self.assertEqual((0, 1, 2), updates.parse_version("v0.1.2"))
        self.assertEqual((0, 1, 2), updates.parse_version("V0.1.2"))
        self.assertEqual((1, 0), updates.parse_version("  1.0  "))
        self.assertEqual((0, 1), updates.parse_version("0.1"))

    def test_parse_rejects_junk_empty_and_non_string(self):
        for bad in ("", "v", "banana", "v0.1-beta", "0.1.x", "1..2", "0.1 a"):
            with self.subTest(bad=bad):
                self.assertIsNone(updates.parse_version(bad))
        self.assertIsNone(updates.parse_version(None))
        self.assertIsNone(updates.parse_version(12))

    def test_compare_padding_equality(self):
        self.assertEqual(0, updates.compare_versions("0.1", "0.1.0"))
        self.assertEqual(0, updates.compare_versions("0.1.0.0", "0.1"))
        self.assertEqual(0, updates.compare_versions("v0.1.1", "0.1.1"))

    def test_compare_ordering(self):
        self.assertEqual(1, updates.compare_versions("0.1.2", "0.1.1"))
        self.assertEqual(-1, updates.compare_versions("0.1.1", "0.1.2"))
        self.assertEqual(1, updates.compare_versions("0.10.0", "0.9.9"))
        self.assertEqual(1, updates.compare_versions("1.0.0", "0.9.9"))

    def test_compare_unparsable_sorts_lowest(self):
        self.assertEqual(-1, updates.compare_versions("banana", "0.1.1"))
        self.assertEqual(1, updates.compare_versions("0.1.1", "banana"))
        self.assertEqual(0, updates.compare_versions("banana", "apple"))

    def test_is_newer_requires_both_parsable(self):
        self.assertTrue(updates.is_newer("v0.1.2", "0.1.1"))
        self.assertTrue(updates.is_newer("0.1.2", "0.1"))
        self.assertFalse(updates.is_newer("0.1.1", "0.1.1"))
        self.assertFalse(updates.is_newer("0.1.0", "0.1.1"))
        self.assertFalse(updates.is_newer("banana", "0.1.1"))
        self.assertFalse(updates.is_newer("0.1.2", "banana"))


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------

class ReleaseSelectionTests(UpdatesTestCase):

    def test_newest_eligible_wins_regardless_of_order(self):
        # 0.1.1 listed BEFORE 0.1.0: selection is by version, not order.
        releases = [
            _release("v0.1.1", _linux_assets("0.1.1")),
            _release("v0.1.0", _linux_assets("0.1.0")),
        ]
        best = updates.newest_linux_release(releases)
        self.assertIsNotNone(best)
        self.assertEqual("0.1.1", best["version"])
        self.assertEqual("https://example.invalid/0.1.1.zip", best["zip_url"])
        self.assertEqual("https://example.invalid/0.1.1.zip.sha256",
                         best["sidecar_url"])
        self.assertEqual("v0.1.1", best["tag"])

    def test_windows_release_filtered_by_asset_name(self):
        releases = [
            _release("v1.0.5", [
                {"name": "AndroidPhotoTransfer-Setup-1.0.5.exe",
                 "browser_download_url": "https://example.invalid/win.exe"},
            ]),
        ]
        self.assertIsNone(updates.newest_linux_release(releases))

    def test_release_without_sidecar_is_not_offered(self):
        # 0.1.2 is newer but has no sidecar: never offered, no blind
        # downloads. 0.1.1 (with sidecar) is the newest eligible.
        releases = [
            _release("v0.1.2", [
                {"name": "android-photo-transfer-linux-0.1.2.zip",
                 "browser_download_url": "https://example.invalid/0.1.2.zip"},
            ]),
            _release("v0.1.1", _linux_assets("0.1.1")),
        ]
        best = updates.newest_linux_release(releases)
        self.assertIsNotNone(best)
        self.assertEqual("0.1.1", best["version"])

    def test_release_with_sidecar_only_is_not_offered(self):
        releases = [
            _release("v0.1.2", [
                {"name": "android-photo-transfer-linux-0.1.2.zip.sha256",
                 "browser_download_url": "https://example.invalid/0.1.2.sha256"},
            ]),
            _release("v0.1.1", _linux_assets("0.1.1")),
        ]
        best = updates.newest_linux_release(releases)
        self.assertIsNotNone(best)
        self.assertEqual("0.1.1", best["version"])

    def test_draft_and_prerelease_excluded(self):
        releases = [
            _release("v0.1.3", _linux_assets("0.1.3"), draft=True),
            _release("v0.1.4", _linux_assets("0.1.4"), prerelease=True),
            _release("v0.1.1", _linux_assets("0.1.1")),
        ]
        best = updates.newest_linux_release(releases)
        self.assertIsNotNone(best)
        self.assertEqual("0.1.1", best["version"])

    def test_version_parsed_from_asset_name_not_tag(self):
        # The tag says v9.9.9 (a Windows tag); the Linux assets say 0.1.5.
        releases = [_release("v9.9.9", _linux_assets("0.1.5"))]
        best = updates.newest_linux_release(releases)
        self.assertIsNotNone(best)
        self.assertEqual("0.1.5", best["version"])
        self.assertEqual("v9.9.9", best["tag"])

    def test_mismatched_zip_and_sidecar_versions_not_paired(self):
        # Zip for 0.1.2, sidecar for 0.1.3: neither version has both.
        releases = [
            _release("v0.1.3", [
                {"name": "android-photo-transfer-linux-0.1.2.zip",
                 "browser_download_url": "https://example.invalid/0.1.2.zip"},
                {"name": "android-photo-transfer-linux-0.1.3.zip.sha256",
                 "browser_download_url": "https://example.invalid/0.1.3.sha256"},
            ]),
        ]
        self.assertIsNone(updates.newest_linux_release(releases))

    def test_no_eligible_release_returns_none(self):
        self.assertIsNone(updates.newest_linux_release([]))
        self.assertIsNone(updates.newest_linux_release(["not a dict", None]))
        self.assertIsNone(updates.newest_linux_release(
            [_release("v0.1.1", None)]))

    def test_find_linux_update_newer_and_current(self):
        releases = [
            _release("v0.1.1", _linux_assets("0.1.1")),
            _release("v0.1.0", _linux_assets("0.1.0")),
        ]
        found = updates.find_linux_update(releases, "0.1.0")
        self.assertIsNotNone(found)
        self.assertEqual("0.1.1", found["version"])
        self.assertIsNone(updates.find_linux_update(releases, "0.1.1"))
        # A dev tree newer than the latest release: no update.
        self.assertIsNone(updates.find_linux_update(releases, "9.9.9"))


# ---------------------------------------------------------------------------
# check_for_update + fetch_releases
# ---------------------------------------------------------------------------

class CheckForUpdateTests(UpdatesTestCase):

    def _releases(self):
        return [
            _release("v0.1.1", _linux_assets("0.1.1")),
            _release("v0.1.0", _linux_assets("0.1.0")),
        ]

    def test_newer_state_carries_release_info(self):
        result = updates.check_for_update(
            "0.1.0", urlopen=_releases_urlopen(self._releases()))
        self.assertEqual("newer", result["state"])
        self.assertEqual("0.1.1", result["version"])
        self.assertEqual("0.1.1", result["latest"])
        self.assertEqual("https://example.invalid/0.1.1.zip", result["zip_url"])
        self.assertEqual("https://example.invalid/0.1.1.zip.sha256",
                         result["sidecar_url"])
        self.assertTrue(result["page_url"])

    def test_current_state(self):
        result = updates.check_for_update(
            "0.1.1", urlopen=_releases_urlopen(self._releases()))
        self.assertEqual("current", result["state"])
        self.assertEqual("0.1.1", result["latest"])

    def test_current_when_running_newer_than_latest(self):
        result = updates.check_for_update(
            "9.9.9", urlopen=_releases_urlopen(self._releases()))
        self.assertEqual("current", result["state"])
        self.assertEqual("0.1.1", result["latest"])

    def test_current_when_no_releases_at_all(self):
        result = updates.check_for_update(
            "0.1.1", urlopen=_releases_urlopen([]))
        self.assertEqual("current", result["state"])
        self.assertIsNone(result["latest"])

    def test_unknown_on_network_failure_carries_plain_reason(self):
        def failing(url, timeout=None):
            raise urllib.error.URLError("boom")

        result = updates.check_for_update("0.1.0", urlopen=failing)
        self.assertEqual("unknown", result["state"])
        self.assertIsNone(result["latest"])
        self.assertIn("GitHub", result["reason"])
        self.assertIn("boom", result["reason"])
        self.assertNotIn("Traceback", result["reason"])

    def test_unknown_on_unparsable_answer(self):
        result = updates.check_for_update(
            "0.1.0", urlopen=_releases_urlopen(b"this is not json"))
        self.assertEqual("unknown", result["state"])
        self.assertIn("did not answer in a form", result["reason"])

    def test_api_base_env_override_and_default(self):
        with mock.patch.dict(os.environ,
                             {"APT_UPDATE_API_BASE": "http://127.0.0.1:9"}):
            self.assertEqual("http://127.0.0.1:9", updates.api_base())
        # An empty override falls back to the default.
        with mock.patch.dict(os.environ, {"APT_UPDATE_API_BASE": ""}):
            self.assertEqual("https://api.github.com", updates.api_base())
        env = dict(os.environ)
        env.pop("APT_UPDATE_API_BASE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual("https://api.github.com", updates.api_base())


class FetchReleasesTests(UpdatesTestCase):

    def test_bad_json_raises_update_error(self):
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.fetch_releases(urlopen=_releases_urlopen(b"not json"))
        self.assertIn("did not answer in a form", str(ctx.exception))

    def test_non_list_json_raises_update_error(self):
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.fetch_releases(
                urlopen=_releases_urlopen({"releases": []}))
        self.assertIn("did not answer in a form", str(ctx.exception))

    def test_timeout_reason_reports_timeout(self):
        def _open(url, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        with self.assertRaises(updates.UpdateError) as ctx:
            updates.fetch_releases(urlopen=_open)
        self.assertIn("timed out", str(ctx.exception))


# ---------------------------------------------------------------------------
# Sidecar parse
# ---------------------------------------------------------------------------

class SidecarTests(UpdatesTestCase):

    def test_valid_line(self):
        sha = "ab" * 32
        self.assertEqual(
            sha,
            updates.parse_sidecar(
                f"{sha}  android-photo-transfer-linux-0.1.1.zip\n"))

    def test_uppercase_hex_normalised(self):
        self.assertEqual("ab" * 32, updates.parse_sidecar("AB" * 32))

    def test_garbage_empty_and_short_return_none(self):
        for bad in ("", "   \n", "not a checksum", "ab" * 31,
                    "zz" * 32, "ab" * 33):
            with self.subTest(bad=bad):
                self.assertIsNone(updates.parse_sidecar(bad))
        self.assertIsNone(updates.parse_sidecar(None))
        self.assertIsNone(updates.parse_sidecar(b"ab" * 32))


# ---------------------------------------------------------------------------
# Download + SHA verify
# ---------------------------------------------------------------------------

class DownloadTests(UpdatesTestCase):

    def _info(self, version="0.1.2"):
        return {"version": version, "zip_url": ZIP_URL,
                "sidecar_url": SIDECAR_URL}

    def _zip_bytes(self, version="0.1.2"):
        path = _make_zip(os.path.join(self.tmp, f"pkg-{version}.zip"), version)
        with open(path, "rb") as fh:
            return fh.read()

    def _sidecar(self, sha, version="0.1.2"):
        return (f"{sha}  android-photo-transfer-linux-{version}.zip\n"
                ).encode()

    def test_matching_sidecar_downloads_and_reports_progress(self):
        zip_bytes = self._zip_bytes()
        sha = _sha256_bytes(zip_bytes)
        urlopen = _stub_urlopen({
            ZIP_URL: zip_bytes,
            SIDECAR_URL: self._sidecar(sha),
        })
        progress = []
        result = updates.download_update(
            self._info(), os.path.join(self.tmp, "dl"),
            progress_cb=lambda done, total: progress.append((done, total)),
            urlopen=urlopen)
        self.assertEqual("0.1.2", result["version"])
        self.assertEqual(sha, result["sha256"])
        self.assertTrue(os.path.isfile(result["zip_path"]))
        with open(result["zip_path"], "rb") as fh:
            self.assertEqual(zip_bytes, fh.read())
        self.assertEqual((0, len(zip_bytes)), progress[0])
        self.assertEqual((len(zip_bytes), len(zip_bytes)), progress[-1])

    def test_mismatch_aborts_and_deletes_file(self):
        zip_bytes = self._zip_bytes()
        sha = _sha256_bytes(zip_bytes)
        urlopen = _stub_urlopen({
            ZIP_URL: b"tampered bytes, not the zip",
            SIDECAR_URL: self._sidecar(sha),
        })
        dest = os.path.join(self.tmp, "dl")
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.download_update(self._info(), dest, urlopen=urlopen)
        self.assertIn("checksum", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))
        self.assertFalse(os.path.exists(
            os.path.join(dest, "android-photo-transfer-linux-0.1.2.zip")))

    def test_missing_sidecar_aborts_and_deletes_file(self):
        urlopen = _stub_urlopen({
            ZIP_URL: self._zip_bytes(),
            SIDECAR_URL: urllib.error.URLError("boom"),
        })
        dest = os.path.join(self.tmp, "dl")
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.download_update(self._info(), dest, urlopen=urlopen)
        self.assertIn("checksum", str(ctx.exception))
        self.assertFalse(os.path.exists(
            os.path.join(dest, "android-photo-transfer-linux-0.1.2.zip")))

    def test_unreadable_sidecar_aborts_and_deletes_file(self):
        urlopen = _stub_urlopen({
            ZIP_URL: self._zip_bytes(),
            SIDECAR_URL: b"garbage, no checksum here\n",
        })
        dest = os.path.join(self.tmp, "dl")
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.download_update(self._info(), dest, urlopen=urlopen)
        self.assertIn("checksum", str(ctx.exception))
        self.assertFalse(os.path.exists(
            os.path.join(dest, "android-photo-transfer-linux-0.1.2.zip")))

    def test_cancel_mid_download_removes_partial_file(self):
        zip_bytes = b"x" * 100
        seen = {"chunks": 0}

        def progress_cb(done, total):
            seen["chunks"] += 1

        # The sidecar URL is deliberately absent from the stub map: a
        # cancelled download must never get as far as fetching it.
        urlopen = _stub_urlopen({
            ZIP_URL: _ChunkedResponse(zip_bytes, chunk=10),
        })
        dest = os.path.join(self.tmp, "dl")
        with self.assertRaises(updates.UpdateCancelled):
            updates.download_update(
                self._info(), dest, progress_cb=progress_cb,
                cancel=lambda: seen["chunks"] >= 1, urlopen=urlopen)
        self.assertEqual(1, seen["chunks"],
                         "cancel should fire after the first chunk")
        self.assertFalse(os.path.exists(
            os.path.join(dest, "android-photo-transfer-linux-0.1.2.zip")))

    def test_missing_urls_abort_without_network(self):
        def guard(url, timeout=None):
            raise AssertionError("no network call should happen")

        dest = os.path.join(self.tmp, "dl")
        cases = [
            {"version": "0.1.2", "sidecar_url": SIDECAR_URL},
            {"version": "0.1.2", "zip_url": ZIP_URL},
            {"zip_url": ZIP_URL, "sidecar_url": SIDECAR_URL},
        ]
        for info in cases:
            with self.subTest(info=info):
                with self.assertRaises(updates.UpdateError):
                    updates.download_update(info, dest, urlopen=guard)


# ---------------------------------------------------------------------------
# Apply (atomic swap)
# ---------------------------------------------------------------------------

class ApplyTests(UpdatesTestCase):

    def _apply(self, zip_path, version, install_dir):
        return updates.apply_update(zip_path, version,
                                    install_dir=install_dir,
                                    python=sys.executable)

    def _read(self, install_dir, rel):
        with open(os.path.join(install_dir, rel)) as fh:
            return fh.read()

    def test_happy_path_swaps_version_and_preserves_keep_list(self):
        install_dir = _make_install(self.tmp)
        zip_path = _make_zip(os.path.join(self.tmp, "pkg-0.1.2.zip"), "0.1.2")
        self._apply(zip_path, "0.1.2", install_dir)
        # The new version is in place; the old files are gone.
        self.assertEqual("0.1.2", updates.read_package_version(install_dir))
        self.assertFalse(os.path.exists(
            os.path.join(install_dir, "android_photo_transfer", "old.py")))
        self.assertTrue(os.path.exists(
            os.path.join(install_dir, "android_photo_transfer", "new.py")))
        self.assertEqual("Android Photo Transfer 0.1.2\n",
                         self._read(install_dir, "README.md"))
        self.assertTrue(os.path.exists(
            os.path.join(install_dir, "icons", "icon.png")))
        # The keep-list survived the swap, contents intact.
        self.assertEqual("HISTORY", self._read(install_dir, "history.db"))
        self.assertEqual("WAL", self._read(install_dir, "history.db-wal"))
        self.assertEqual("SHM", self._read(install_dir, "history.db-shm"))
        self.assertEqual("LOGS", self._read(install_dir, "logs/app.log"))
        self.assertEqual("# fake venv\n",
                         self._read(install_dir, "venv/bin/activate"))
        # The launcher is still there and still executable.
        launcher = os.path.join(install_dir, "android-photo-transfer")
        self.assertTrue(os.path.isfile(launcher))
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue(os.stat(launcher).st_mode & 0o111)
        # No backup or staging leftovers beside the install.
        self.assertEqual([], _leftovers(self.tmp))

    def test_version_mismatch_aborts_and_leaves_install_untouched(self):
        install_dir = _make_install(self.tmp)
        before = sorted(os.listdir(install_dir))
        zip_path = _make_zip(os.path.join(self.tmp, "pkg-9.9.9.zip"), "9.9.9")
        with self.assertRaises(updates.UpdateError) as ctx:
            self._apply(zip_path, "0.1.2", install_dir)
        self.assertIn("9.9.9", str(ctx.exception))
        self.assertIn("0.1.2", str(ctx.exception))
        self.assertEqual(before, sorted(os.listdir(install_dir)))
        self.assertEqual("0.1.1", updates.read_package_version(install_dir))
        self.assertEqual("HISTORY", self._read(install_dir, "history.db"))
        self.assertTrue(os.path.exists(
            os.path.join(install_dir, "android_photo_transfer", "old.py")))
        self.assertEqual([], _leftovers(self.tmp))

    def test_missing_version_in_zip_aborts(self):
        install_dir = _make_install(self.tmp)
        zip_path = os.path.join(self.tmp, "pkg-noversion.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("android_photo_transfer/__init__.py",
                        "# no version here\n")
        with self.assertRaises(updates.UpdateError) as ctx:
            self._apply(zip_path, "0.1.2", install_dir)
        self.assertIn("version", str(ctx.exception))
        self.assertEqual("0.1.1", updates.read_package_version(install_dir))
        self.assertEqual([], _leftovers(self.tmp))

    def test_damaged_zip_aborts_and_leaves_install_untouched(self):
        install_dir = _make_install(self.tmp)
        zip_path = os.path.join(self.tmp, "pkg-damaged.zip")
        with open(zip_path, "wb") as fh:
            fh.write(b"this is not a zip file")
        with self.assertRaises(updates.UpdateError) as ctx:
            self._apply(zip_path, "0.1.2", install_dir)
        self.assertIn("damaged", str(ctx.exception))
        self.assertEqual("0.1.1", updates.read_package_version(install_dir))
        self.assertEqual("HISTORY", self._read(install_dir, "history.db"))
        self.assertEqual([], _leftovers(self.tmp))

    def test_unsafe_zip_entry_aborts_and_writes_nothing_outside(self):
        install_dir = _make_install(self.tmp)
        zip_path = os.path.join(self.tmp, "pkg-unsafe.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("android_photo_transfer/__init__.py",
                        '__version__ = "0.1.2"\n')
            zf.writestr("../evil.py", "EVIL = True\n")
        with self.assertRaises(updates.UpdateError) as ctx:
            self._apply(zip_path, "0.1.2", install_dir)
        self.assertIn("unsafe", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.py")))
        self.assertEqual("0.1.1", updates.read_package_version(install_dir))
        self.assertEqual([], _leftovers(self.tmp))

    def test_missing_zip_file_aborts(self):
        install_dir = _make_install(self.tmp)
        with self.assertRaises(updates.UpdateError) as ctx:
            self._apply(os.path.join(self.tmp, "no-such.zip"), "0.1.2",
                        install_dir)
        self.assertIn("missing", str(ctx.exception))
        self.assertEqual("0.1.1", updates.read_package_version(install_dir))

    def test_non_installed_dir_aborts(self):
        # A source tree (package, no launcher) is never updated in place.
        tree = os.path.join(self.tmp, "source-tree")
        pkg = os.path.join(tree, "android_photo_transfer")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write('__version__ = "0.1.1"\n')
        zip_path = _make_zip(os.path.join(self.tmp, "pkg-0.1.2.zip"), "0.1.2")
        with self.assertRaises(updates.UpdateError) as ctx:
            self._apply(zip_path, "0.1.2", tree)
        self.assertIn("not installed", str(ctx.exception))
        self.assertEqual("0.1.1", updates.read_package_version(tree))
        self.assertEqual([], _leftovers(self.tmp))


# ---------------------------------------------------------------------------
# Installed detection + read_package_version + describe()
# ---------------------------------------------------------------------------

class InstalledDetectionTests(UpdatesTestCase):

    def test_installed_true_with_launcher(self):
        install_dir = _make_install(self.tmp)
        pkg_dir = os.path.join(install_dir, "android_photo_transfer")
        self.assertTrue(updates.is_installed(pkg_dir))

    def test_installed_false_on_source_tree(self):
        # The real port tree has a package dir but no launcher beside it.
        pkg_dir = os.path.dirname(os.path.dirname(
            os.path.abspath(updates.__file__)))
        self.assertFalse(updates.is_installed(pkg_dir))

    def test_launcher_path_and_find_install_dir(self):
        install_dir = _make_install(self.tmp)
        pkg_dir = os.path.join(install_dir, "android_photo_transfer")
        self.assertEqual(install_dir, updates.find_install_dir(pkg_dir))
        self.assertEqual(os.path.join(install_dir, "android-photo-transfer"),
                         updates.launcher_path(install_dir))

    def test_read_package_version(self):
        install_dir = _make_install(self.tmp, version="0.1.1")
        self.assertEqual("0.1.1", updates.read_package_version(install_dir))
        tree = os.path.join(self.tmp, "quoted")
        os.makedirs(os.path.join(tree, "android_photo_transfer"))
        with open(os.path.join(tree, "android_photo_transfer",
                               "__init__.py"), "w") as fh:
            fh.write("__version__ = '2.0.0'\n")
        self.assertEqual("2.0.0", updates.read_package_version(tree))

    def test_read_package_version_missing_or_versionless(self):
        self.assertIsNone(updates.read_package_version(
            os.path.join(self.tmp, "no-such-tree")))
        tree = os.path.join(self.tmp, "versionless")
        os.makedirs(os.path.join(tree, "android_photo_transfer"))
        with open(os.path.join(tree, "android_photo_transfer",
                               "__init__.py"), "w") as fh:
            fh.write("# no version here\n")
        self.assertIsNone(updates.read_package_version(tree))


class DescribeTests(UpdatesTestCase):

    def test_describe_passes_update_errors_through_verbatim(self):
        if describe is None:
            self.skipTest("android_photo_transfer.core.errors not built yet")
        self.assertEqual("plain words",
                         describe(updates.UpdateError("plain words")))
        self.assertEqual("cancelled",
                         describe(updates.UpdateCancelled("cancelled")))


if __name__ == "__main__":
    unittest.main()
