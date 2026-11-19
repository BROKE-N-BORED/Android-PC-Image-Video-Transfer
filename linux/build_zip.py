#!/usr/bin/env python3
"""Build the Android Photo Transfer Linux release zip.

Usage (from the port/ directory):
    python3 build_zip.py [--version 0.1.1] [--out dist/]

The zip holds the app + installer (no tests, no dev files):
    android_photo_transfer/   the app package
    icons/                    app icons
    install.sh                terminal installer (bash)
    install_gui.py            GUI installer
    install_lib.py            shared install steps
    android-photo-transfer.desktop
    install-android-photo-transfer.desktop
    README.md

Zip mode rules (AGENTS.md lesson -- GUI extractors drop the execute bit
when the file-type bits are missing):
  - every entry's external_attr MUST include the file-type bits:
    0o100755 for scripts, 0o100644 for plain files, 0o40755 for dirs.
  - build_zip.py SELF-VERIFIES the stored modes after writing, and exits
    nonzero on any mismatch.
No network calls. Works from a clean copy of the tree.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
import zipfile

PORT_DIR = os.path.dirname(os.path.abspath(__file__))

# (tree path, mode) -- mode includes the file-type bits.
SCRIPT_MODE = 0o100755
FILE_MODE = 0o100644
DIR_MODE = 0o40755

SCRIPTS = {"install.sh"}
TOP_FILES = (
    "install.sh",
    "install_gui.py",
    "install_lib.py",
    "android-photo-transfer.desktop",
    "install-android-photo-transfer.desktop",
    "README.md",
)
TOP_DIRS = ("android_photo_transfer", "icons")

SKIP_DIRS = {"__pycache__", ".git", ".forge"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".zip", ".tmp", ".partial")


def collect_files() -> list[tuple[str, str, int]]:
    """(archive name, filesystem path, mode) for every zip entry."""
    entries: list[tuple[str, str, int]] = []
    for name in TOP_FILES:
        path = os.path.join(PORT_DIR, name)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"missing release file: {name}")
        mode = SCRIPT_MODE if name in SCRIPTS else FILE_MODE
        entries.append((name, path, mode))
    for top in TOP_DIRS:
        base = os.path.join(PORT_DIR, top)
        if not os.path.isdir(base):
            raise FileNotFoundError(f"missing release dir: {top}")
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            rel_dir = os.path.relpath(root, PORT_DIR)
            entries.append((rel_dir + "/", root, DIR_MODE))
            for filename in sorted(files):
                if filename.endswith(SKIP_SUFFIXES):
                    continue
                path = os.path.join(root, filename)
                rel = os.path.relpath(path, PORT_DIR)
                entries.append((rel, path, FILE_MODE))
    entries.sort(key=lambda e: e[0])
    return entries


def build_zip(version: str, out_dir: str) -> str:
    entries = collect_files()
    os.makedirs(out_dir, exist_ok=True)
    zip_name = f"android-photo-transfer-linux-{version}.zip"
    zip_path = os.path.join(out_dir, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path, mode in entries:
            info = zipfile.ZipInfo(arcname)
            info.external_attr = (mode & 0xFFFF) << 16
            if arcname.endswith("/"):
                info.file_size = 0
                zf.writestr(info, b"")
            else:
                with open(path, "rb") as fh:
                    zf.writestr(info, fh.read())
    return zip_path


def verify_zip(zip_path: str, entries: list[tuple[str, str, int]]) -> list[str]:
    """Check the stored modes of every entry. Returns a list of problems."""
    expected = {arcname: mode for arcname, _path, mode in entries}
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if sorted(names) != sorted(expected):
            problems.append(
                f"entry list mismatch: in zip but unexpected: "
                f"{sorted(set(names) - set(expected))}; missing: "
                f"{sorted(set(expected) - set(names))}")
        for info in zf.infolist():
            stored = (info.external_attr >> 16) & 0xFFFF
            want = expected.get(info.filename)
            if want is None:
                continue
            if stored != want:
                problems.append(
                    f"{info.filename}: stored mode {oct(stored)}, "
                    f"expected {oct(want)}")
            if stat.S_ISREG(want) and not stat.S_ISREG(stored):
                problems.append(f"{info.filename}: file-type bits missing "
                                f"(stored {oct(stored)})")
    return problems


def write_sidecar(zip_path: str) -> str:
    """Write the .sha256 sidecar next to the zip (standard sha256sum line:
    "<hex>  <zip basename>"). The updater refuses a release without one,
    so the two are always published together."""
    digest = hashlib.sha256()
    with open(zip_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    sidecar_path = zip_path + ".sha256"
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        fh.write(f"{digest.hexdigest()}  {os.path.basename(zip_path)}\n")
    return sidecar_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the release zip.")
    parser.add_argument("--version", default="0.1.1",
                        help="release version stamped into the zip name")
    parser.add_argument("--out", default=os.path.join(PORT_DIR, "dist"),
                        help="output directory (default port/dist/)")
    args = parser.parse_args(argv)

    entries = collect_files()
    zip_path = build_zip(args.version, args.out)
    files = sum(1 for a, _p, m in entries if stat.S_ISREG(m))
    dirs = sum(1 for a, _p, m in entries if stat.S_ISDIR(m))
    print(f"Wrote {zip_path}")
    print(f"  {files} files + {dirs} directory entries "
          f"= {len(entries)} total entries")
    problems = verify_zip(zip_path, entries)
    if problems:
        print("SELF-CHECK FAILED -- stored modes are wrong:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    scripts = [a for a, _p, m in entries if m == SCRIPT_MODE]
    print(f"  self-check OK: all {len(entries)} stored modes correct "
          f"(executable: {', '.join(scripts) or 'none'})")
    sidecar_path = write_sidecar(zip_path)
    print(f"Wrote {sidecar_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
