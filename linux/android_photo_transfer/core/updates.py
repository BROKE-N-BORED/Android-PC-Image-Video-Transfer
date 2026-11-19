"""Standing-order updater for the Android Photo Transfer Linux port (v0.1.1).

Qt-free, stdlib only, so the UI layer can drive it from a worker thread
and the tests can run it without Qt. The shape follows Pixel Forge v0.69
and ACR v0.12: one unauthenticated GET to the public GitHub releases
endpoint, no token, no identifying information sent.

The repo hosts BOTH Windows releases (v1.0.x) and Linux releases
(v0.1.x), so selection is by ASSET NAME, never by tag: a release only
counts when it carries BOTH android-photo-transfer-linux-<ver>.zip AND
its .zip.sha256 sidecar for the same version. A release without the
sidecar is never offered (no blind downloads, ever). The version is
parsed from the asset name.

Download: the zip is streamed to a temp dir, then SHA-256-checked
against the sidecar. A mismatch deletes the download and aborts.

Apply: the zip is extracted to a temp dir, the extracted package's
__version__ is checked against the release version, then the install is
replaced by an ATOMIC SWAP: the new tree is built in a temp dir beside
the install dir, the keep-list (venv, history.db*, logs -- the same
list install_lib.copy_tree preserves) is moved into it, the install dir
is renamed to a backup and the staging dir renamed into its place, and
the backup deleted. Any failure before the swap leaves the old install
untouched; a failed start-up check after the swap rolls the backup back.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

log = logging.getLogger("android_photo_transfer")

REPO = "BROKE-N-BORED/Android-PC-Image-Video-Transfer"
DEFAULT_API_BASE = "https://api.github.com"
API_BASE_ENV = "APT_UPDATE_API_BASE"
TIMEOUT_S = 8
DOWNLOAD_CHUNK = 65536
VERIFY_TIMEOUT_S = 120
LAUNCHER_NAME = "android-photo-transfer"
ZIP_RE = re.compile(r"^android-photo-transfer-linux-(\d+(?:\.\d+)*)\.zip$")
VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
# What the updater preserves across the swap (mirrors install_lib.copy_tree).
KEEP_NAMES = ("venv", "history.db", "history.db-wal", "history.db-shm", "logs")
# What the release zip contributes to the install tree (mirrors copy_tree).
TREE_DIRS = ("android_photo_transfer", "icons")
TREE_FILES = ("README.md",)


class UpdateError(Exception):
    """An update step failed. The message is already plain language."""


class UpdateCancelled(Exception):
    """The user cancelled the download."""


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def parse_version(text):
    """A version string as a tuple of ints ("0.1.1" -> (0, 1, 1), "v0.1.1"
    the same), or None when it is not a version at all. Anything but an
    optional "v" followed by numbers and dots is refused."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    if not text:
        return None
    numbers = []
    for part in text.split("."):
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return tuple(numbers)


def compare_versions(a, b):
    """-1 when a < b, 0 when equal, 1 when a > b. Shorter tuples are padded
    with zeros, so 0.1 and 0.1.0 compare equal. An unparsable version
    compares as lower than any parsable one; two unparsable versions are
    equal."""
    parsed_a = parse_version(a)
    parsed_b = parse_version(b)
    if parsed_a is None and parsed_b is None:
        return 0
    if parsed_a is None:
        return -1
    if parsed_b is None:
        return 1
    width = max(len(parsed_a), len(parsed_b))
    padded_a = parsed_a + (0,) * (width - len(parsed_a))
    padded_b = parsed_b + (0,) * (width - len(parsed_b))
    return (padded_a > padded_b) - (padded_a < padded_b)


def is_newer(latest, current):
    """True only when both versions parse and latest is newer. An unknown
    version is never reported as an update."""
    if parse_version(latest) is None or parse_version(current) is None:
        return False
    return compare_versions(latest, current) > 0


# ---------------------------------------------------------------------------
# Release discovery
# ---------------------------------------------------------------------------

def api_base():
    """GitHub API base, overridable for tests via APT_UPDATE_API_BASE."""
    return os.environ.get(API_BASE_ENV) or DEFAULT_API_BASE


def releases_url(base=None):
    return (base or api_base()).rstrip("/") + f"/repos/{REPO}/releases"


def fetch_releases(base=None, urlopen=None):
    """The releases list from GitHub. Raises UpdateError (plain words)
    when the network fails, the answer is not JSON, or it is not a list.
    urlopen and base are test seams: pass a stub or a fake endpoint and
    no real network is touched."""
    opener = urlopen or urllib.request.urlopen
    target = releases_url(base)
    try:
        with opener(target, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise UpdateError("The update check timed out.")
        raise UpdateError(f"Could not reach GitHub: {reason}")
    except TimeoutError:
        raise UpdateError("The update check timed out.")
    except (ValueError, UnicodeDecodeError):
        raise UpdateError(
            "The releases page did not answer in a form the app understands.")
    if not isinstance(payload, list):
        raise UpdateError(
            "The releases page did not answer in a form the app understands.")
    return payload


def _assets_by_version(release):
    """{version: {"zip": url, "sidecar": url}} for the Linux assets in one
    release, paired by the version in the asset name."""
    found: dict[str, dict[str, str]] = {}
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        return found
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name") or ""
        link = asset.get("browser_download_url") or ""
        if not link:
            continue
        if name.endswith(".zip.sha256"):
            match = ZIP_RE.match(name[: -len(".sha256")])
            if match:
                found.setdefault(match.group(1), {})["sidecar"] = link
        else:
            match = ZIP_RE.match(name)
            if match:
                found.setdefault(match.group(1), {})["zip"] = link
    return found


def newest_linux_release(releases):
    """The newest eligible Linux release, or None.

    Eligible means: not a draft, not a prerelease, and carrying BOTH the
    zip and its .sha256 sidecar for the same version. Windows releases
    are filtered out by asset name (they carry no Linux zip). Returns
    {version, tag, zip_url, sidecar_url, page_url}."""
    best = None
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        for version, links in _assets_by_version(release).items():
            if "zip" not in links or "sidecar" not in links:
                continue
            if parse_version(version) is None:
                continue
            if best is not None and compare_versions(version, best["version"]) <= 0:
                continue
            best = {
                "version": version,
                "tag": release.get("tag_name") or version,
                "zip_url": links["zip"],
                "sidecar_url": links["sidecar"],
                "page_url": release.get("html_url") or "",
            }
    return best


def find_linux_update(releases, current_version):
    """The update info for the newest eligible Linux release newer than
    the running version, or None when there is none."""
    best = newest_linux_release(releases)
    if best is None:
        return None
    if not is_newer(best["version"], current_version):
        return None
    return best


def check_for_update(current_version, base=None, urlopen=None):
    """One call, three outcomes, never an exception for a network or
    parsing problem:

    {"state": "newer", "version", "tag", "zip_url", "sidecar_url",
     "page_url", "latest"} -- a newer Linux release is out.
    {"state": "current", "latest": <newest Linux release or None>} --
     the running version is the newest (or newer than the latest
     release, a dev tree).
    {"state": "unknown", "latest": None, "reason": <plain words>} --
     the check could not run.
    """
    try:
        releases = fetch_releases(base=base, urlopen=urlopen)
    except UpdateError as exc:
        return {"state": "unknown", "latest": None, "reason": str(exc)}
    newest = newest_linux_release(releases)
    latest = newest["version"] if newest else None
    if newest is not None and is_newer(newest["version"], current_version):
        result = dict(newest)
        result["state"] = "newer"
        result["latest"] = newest["version"]
        return result
    return {"state": "current", "latest": latest}


# ---------------------------------------------------------------------------
# Verified download
# ---------------------------------------------------------------------------

def parse_sidecar(text):
    """The checksum from a .sha256 sidecar (standard sha256sum line:
    "<hex>  <filename>"), or None when the text carries no valid SHA-256."""
    if not isinstance(text, str):
        return None
    tokens = text.split()
    if not tokens:
        return None
    candidate = tokens[0].strip().lower()
    if len(candidate) != 64:
        return None
    if any(char not in "0123456789abcdef" for char in candidate):
        return None
    return candidate


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download_file(url, dest_path, opener, progress_cb, cancel):
    """Stream one URL to a file. progress_cb(downloaded, total) fires per
    chunk, total None when the server did not say. cancel() is checked
    between chunks; a cancelled download raises UpdateCancelled. Network
    trouble raises UpdateError with a plain reason."""
    try:
        response = opener(url, timeout=TIMEOUT_S)
    except urllib.error.URLError as exc:
        raise UpdateError(f"Could not reach the download: {exc.reason}")
    except TimeoutError:
        raise UpdateError("The download timed out.")
    try:
        with response as resp:
            total = None
            headers = getattr(resp, "headers", None)
            if headers is not None:
                length = headers.get("Content-Length")
                if length:
                    try:
                        total = int(length)
                    except ValueError:
                        total = None
            done = 0
            if progress_cb is not None:
                progress_cb(0, total)
            with open(dest_path, "wb") as fh:
                while True:
                    if cancel is not None and cancel():
                        raise UpdateCancelled()
                    try:
                        chunk = resp.read(DOWNLOAD_CHUNK)
                    except TimeoutError:
                        raise UpdateError("The download timed out.")
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None:
                        progress_cb(done, total)
    except urllib.error.URLError as exc:
        raise UpdateError(f"The download failed: {exc.reason}")


def _download_text(url, opener):
    """One small URL as text (the sidecar)."""
    try:
        with opener(url, timeout=TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise UpdateError(f"Could not fetch the checksum file: {exc.reason}")
    except TimeoutError:
        raise UpdateError("The download timed out.")
    except (ValueError, UnicodeDecodeError):
        raise UpdateError("The checksum file did not read as text.")


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download_update(info, dest_dir, progress_cb=None, cancel=None, urlopen=None):
    """Download a newer release and check it against its checksum sidecar.
    Returns {"zip_path", "version", "sha256"}. A mismatch, a missing or
    unreadable sidecar, or a missing download file raises UpdateError and
    the download is thrown away: nothing unverified ever reaches the
    apply step. A cancelled download raises UpdateCancelled and its
    partial file is removed."""
    zip_url = info.get("zip_url")
    sidecar_url = info.get("sidecar_url")
    version = info.get("version") or info.get("latest")
    if not zip_url:
        raise UpdateError("The release has no download file.")
    if not sidecar_url:
        raise UpdateError(
            "The release has no checksum file, so the download cannot be checked.")
    if not version:
        raise UpdateError("The release did not name a version.")
    os.makedirs(dest_dir, exist_ok=True)
    zip_name = f"android-photo-transfer-linux-{version}.zip"
    zip_path = os.path.join(dest_dir, zip_name)
    opener = urlopen or urllib.request.urlopen
    try:
        _download_file(zip_url, zip_path, opener, progress_cb, cancel)
        sidecar = _download_text(sidecar_url, opener)
    except (UpdateCancelled, UpdateError):
        _remove_quietly(zip_path)
        raise
    want = parse_sidecar(sidecar)
    if want is None:
        _remove_quietly(zip_path)
        raise UpdateError(
            "The release checksum file did not contain a checksum, "
            "so the download was not used.")
    actual = _sha256_file(zip_path)
    if actual != want:
        _remove_quietly(zip_path)
        raise UpdateError(
            "The downloaded file does not match the release checksum, "
            "so it was not used.")
    return {"zip_path": zip_path, "version": version, "sha256": actual}


# ---------------------------------------------------------------------------
# Installed detection
# ---------------------------------------------------------------------------

def package_dir():
    """The android_photo_transfer package directory (this file's grandparent)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_install_dir(pkg_dir=None):
    """The install dir: the package's parent directory."""
    return os.path.dirname(pkg_dir or package_dir())


def launcher_path(install_dir=None):
    return os.path.join(install_dir or find_install_dir(), LAUNCHER_NAME)


def is_installed(pkg_dir=None):
    """True when the app runs from an install dir: the package's parent
    dir holds the launcher script. A source checkout has no launcher file
    there, so it is never mistaken for an install."""
    return os.path.isfile(launcher_path(find_install_dir(pkg_dir)))


# ---------------------------------------------------------------------------
# Apply (atomic swap)
# ---------------------------------------------------------------------------

def read_package_version(tree_dir):
    """__version__ from a tree's android_photo_transfer/__init__.py, or
    None. Read by regex, never imported (the tree is untrusted until
    checked)."""
    init_path = os.path.join(tree_dir, "android_photo_transfer", "__init__.py")
    try:
        with open(init_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def _safe_zip_entry(name):
    """False for absolute paths and .. escapes."""
    if not name or os.path.isabs(name):
        return False
    parts = name.replace("\\", "/").split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _extract_zip(zip_path, dest_dir):
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _safe_zip_entry(info.filename):
                raise UpdateError(
                    "The update package contains an unsafe file path, "
                    "so it was not used.")
            target = os.path.join(
                dest_dir, *info.filename.replace("\\", "/").split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, DOWNLOAD_CHUNK)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode)


def _launcher_content(python):
    """The launcher script, identical to install_lib.write_launcher."""
    return (
        "#!/bin/sh\n"
        "# Launcher written by the Android Photo Transfer installer.\n"
        "DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
        "cd \"$DIR\" || exit 1\n"
        f"exec \"{python}\" -m android_photo_transfer \"$@\"\n"
    )


def _verify_tree(install_dir, python=None):
    """Run `<python> -c "import android_photo_transfer"` from the tree.
    The tree's venv python is used when it exists, else the given python,
    else the running interpreter. Raises UpdateError on failure."""
    venv_python = os.path.join(install_dir, "venv", "bin", "python")
    if os.path.isfile(venv_python):
        python = venv_python
    elif python is None:
        python = sys.executable
    try:
        result = subprocess.run(
            [python, "-c", "import android_photo_transfer"],
            cwd=install_dir, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=VERIFY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any failure is a failed check
        raise UpdateError(f"Could not check the updated app ({exc}).")
    if result.returncode != 0:
        raise UpdateError("The updated app did not pass its start-up check.")
    return True


def _rollback(install_dir, backup_dir):
    """Put the backup tree back after a failed post-swap check. Moves the
    keep-list back first (it lives in the new tree), then swaps the dirs.
    Returns True when the old install is back in place."""
    for name in KEEP_NAMES:
        src = os.path.join(install_dir, name)
        if os.path.exists(src):
            try:
                shutil.move(src, os.path.join(backup_dir, name))
            except OSError:
                log.warning("Rollback could not move %s back", name)
    shutil.rmtree(install_dir, ignore_errors=True)
    try:
        os.rename(backup_dir, install_dir)
    except OSError:
        log.error("Rollback could not restore the old install; it is at %s",
                  backup_dir)
        return False
    return True


def apply_update(zip_path, version, install_dir=None, python=None):
    """Replace the install with a verified update zip, by atomic swap.

    1. Extract to a temp dir beside the install dir; check the extracted
       package's __version__ matches the release version.
    2. Build the new tree in a staging dir beside the install dir (the
       zip's app files, plus the old launcher, whose paths stay right
       because the install dir path does not change).
    3. Move the keep-list (venv, history.db*, logs) into staging.
    4. Swap: rename the install dir to a backup, rename staging into its
       place, delete the backup.
    5. Verify the new tree imports; on failure roll the backup back.

    Raises UpdateError with a plain reason. Any failure before the swap
    leaves the old install untouched (the keep-list is moved back)."""
    install_dir = install_dir or find_install_dir()
    if not os.path.isfile(os.path.join(install_dir, LAUNCHER_NAME)):
        raise UpdateError(
            "This copy of the app is not installed, so it cannot update itself.")
    if not os.path.isfile(zip_path):
        raise UpdateError("The downloaded update file is missing.")
    parent = os.path.dirname(install_dir)
    extract_dir = None
    staging_dir = None
    try:
        extract_dir = tempfile.mkdtemp(prefix=".apt-update-extract-", dir=parent)
        staging_dir = tempfile.mkdtemp(prefix=".apt-update-new-", dir=parent)
    except OSError as exc:
        raise UpdateError(f"Could not make room for the update ({exc}).")
    backup_dir = os.path.join(parent, f".apt-update-backup-{os.getpid()}")
    if os.path.exists(backup_dir):
        backup_dir = os.path.join(
            parent, f".apt-update-backup-{os.getpid()}-{int(time.time())}")
    moved: list[str] = []
    swapped = False
    rolled_back = False
    try:
        # 1. Extract + sanity-check the package version (before touching
        #    anything in the install).
        try:
            _extract_zip(zip_path, extract_dir)
        except zipfile.BadZipFile:
            raise UpdateError("The downloaded update file is damaged.")
        except OSError as exc:
            raise UpdateError(f"Could not unpack the update ({exc}).")
        found_version = read_package_version(extract_dir)
        if found_version is None:
            raise UpdateError(
                "The update package did not say what version it is, "
                "so it was not used.")
        if compare_versions(found_version, version) != 0:
            raise UpdateError(
                f"The update package says it is version {found_version}, "
                f"but the release says {version}, so it was not used.")
        package_src = os.path.join(extract_dir, "android_photo_transfer")
        if not os.path.isdir(package_src):
            raise UpdateError(
                "The update package does not contain the app, so it was not used.")
        # 2. Build the new tree in staging.
        try:
            shutil.copytree(package_src,
                            os.path.join(staging_dir, "android_photo_transfer"))
            icons_src = os.path.join(extract_dir, "icons")
            if os.path.isdir(icons_src):
                shutil.copytree(icons_src, os.path.join(staging_dir, "icons"))
            readme_src = os.path.join(extract_dir, "README.md")
            if os.path.isfile(readme_src):
                shutil.copy2(readme_src, os.path.join(staging_dir, "README.md"))
        except OSError as exc:
            raise UpdateError(f"Could not prepare the update ({exc}).")
        # 3. Launcher: copy the old one (its paths stay right), or write a
        #    fresh one from the installer template.
        old_launcher = os.path.join(install_dir, LAUNCHER_NAME)
        new_launcher = os.path.join(staging_dir, LAUNCHER_NAME)
        try:
            shutil.copy2(old_launcher, new_launcher)
            os.chmod(new_launcher, 0o755)
        except OSError:
            venv_python = os.path.join(install_dir, "venv", "bin", "python")
            if os.path.isfile(venv_python):
                launcher_python = venv_python
            else:
                launcher_python = python or sys.executable
            try:
                with open(new_launcher, "w", encoding="utf-8") as fh:
                    fh.write(_launcher_content(launcher_python))
                os.chmod(new_launcher, 0o755)
            except OSError as exc:
                raise UpdateError(f"Could not write the app launcher ({exc}).")
        # 4. Move the keep-list into staging.
        for name in KEEP_NAMES:
            src = os.path.join(install_dir, name)
            if os.path.exists(src):
                try:
                    shutil.move(src, os.path.join(staging_dir, name))
                except OSError as exc:
                    raise UpdateError(
                        f"Could not move the app's saved data ({exc}).")
                moved.append(name)
        # 5. The swap: two renames on the same filesystem.
        try:
            os.rename(install_dir, backup_dir)
        except OSError as exc:
            raise UpdateError(f"Could not replace the app files ({exc}).")
        try:
            os.rename(staging_dir, install_dir)
        except OSError:
            try:
                os.rename(backup_dir, install_dir)
            except OSError:
                log.error("Swap failed and the old install could not be put "
                          "back; it is at %s", backup_dir)
            raise UpdateError(
                "Could not replace the app files, so the old version "
                "was left in place.")
        swapped = True
        # 6. Verify the new tree starts; roll back on failure.
        try:
            _verify_tree(install_dir, python=python)
        except UpdateError:
            if _rollback(install_dir, backup_dir):
                rolled_back = True
                raise UpdateError(
                    "The updated app did not pass its start-up check, "
                    "so the old version was put back.")
            raise UpdateError(
                "The updated app did not pass its start-up check, and the "
                "old version could not be put back. Reinstall the app from "
                "its release zip.")
    except UpdateError:
        if not swapped:
            # The old install is still in place: move the keep-list back.
            for name in moved:
                src = os.path.join(staging_dir, name)
                if os.path.exists(src):
                    try:
                        shutil.move(src, os.path.join(install_dir, name))
                    except OSError:
                        log.warning("Could not move %s back after a failed update",
                                    name)
        raise
    finally:
        if extract_dir is not None:
            shutil.rmtree(extract_dir, ignore_errors=True)
        if swapped and not rolled_back:
            shutil.rmtree(backup_dir, ignore_errors=True)
        elif not swapped and staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
