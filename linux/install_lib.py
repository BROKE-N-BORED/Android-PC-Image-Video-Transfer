#!/usr/bin/env python3
"""Shared installer logic for Android Photo Transfer (Linux port).

Single source of the install steps, used by both install.sh (which just
runs this file) and install_gui.py (which drives it from a window).

Install layout (honours $HOME, so a clean-HOME test works):
    ~/.local/share/android-photo-transfer/   the app tree + launcher
    ~/.local/share/applications/android-photo-transfer.desktop
    ~/.local/share/icons/... (not used; the menu entry points at the
    installed 256 px PNG by absolute path)

Steps, in order:
  1. Find a Python that can `import PySide6`: the existing install venv
     first, then `python3` on PATH. The RESOLVED path that passed is
     recorded into the launcher.
  2. Only if none passes, provision one: try `pkexec apt install -y
     python3-pyside6` (Mint), else create a venv and `pip install PySide6`
     (needs network). Skipped in the sandbox test, where a PySide6-capable
     python3 is put on PATH.
  3. Copy the app tree into the install dir (re-runnable: the old tree is
     removed first, except a working venv, which is preserved).
  4. Write the launcher script and the menu entry.
  5. Check libmtp; if missing, warn plainly that real-phone connect needs
     it (`sudo apt install libmtp9`) but DO NOT fail (folder mode works).
  6. Verify: run `<python> -c "import android_photo_transfer"` from the
     install dir. Nonzero exit + plain message on failure.

Warn-never-refuse: anything that is not the core copy + verify only warns.
No secrets anywhere. Stdlib only, so install.sh can run it with any
python3 even before PySide6 exists.

CLI (used by install.sh and by the sandbox install test):
    python3 install_lib.py [--install-dir DIR] [--home DIR]
                           [--no-provision] [--no-menu]
"""
from __future__ import annotations

import ctypes.util
import os
import shutil
import subprocess
import sys
import tempfile

APP_ID = "android-photo-transfer"
APP_NAME = "Android Photo Transfer"


def log(msg: str) -> None:
    print(msg, flush=True)


def source_dir() -> str:
    """The extracted tree root (this file lives in it)."""
    return os.path.dirname(os.path.abspath(__file__))


def home_dir() -> str:
    return os.path.expanduser("~")


def default_install_dir(home: str | None = None) -> str:
    return os.path.join(home or home_dir(), ".local", "share", APP_ID)


def python_can_import_pyside6(python: str) -> bool:
    """Run `<python> -c "import PySide6"`; True only on a clean exit."""
    try:
        result = subprocess.run(
            [python, "-c", "import PySide6"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return result.returncode == 0
    except Exception:
        return False


def find_usable_python(install_dir: str) -> str | None:
    """First candidate that passes the PySide6 import check.

    Order: existing install venv, then `python3` on PATH. Returns the
    resolved absolute path, or None.
    """
    candidates: list[str] = []
    venv_python = os.path.join(install_dir, "venv", "bin", "python")
    if os.path.isfile(venv_python):
        candidates.append(venv_python)
    system = shutil.which("python3")
    if system:
        candidates.append(system)
    for candidate in candidates:
        # abspath, NOT realpath: a venv's bin/python is a symlink to the
        # system python, and resolving it would lose the venv's own
        # packages (AGENTS.md: venv pythons defeat realpath checks).
        resolved = os.path.abspath(candidate)
        if python_can_import_pyside6(resolved):
            return resolved
    return None


def provision_python(install_dir: str, log_fn=log) -> str | None:
    """Provision a PySide6-capable Python. Returns its path or None.

    1. pkexec apt install -y python3-pyside6 (Mint system package), then
       re-test system python3.
    2. Otherwise a venv in the install dir + pip install PySide6.
    """
    system = shutil.which("python3")
    pkexec = shutil.which("pkexec")
    apt = shutil.which("apt")
    if pkexec and apt and system:
        log_fn("PySide6 is not installed. Trying to install it with apt "
               "(you may be asked for your password)...")
        try:
            result = subprocess.run(
                [pkexec, "apt", "install", "-y", "python3-pyside6"],
                timeout=600)
            if result.returncode == 0 and python_can_import_pyside6(system):
                log_fn("Installed python3-pyside6.")
                return os.path.abspath(system)
        except Exception as exc:  # noqa: BLE001 - warn and fall through
            log_fn(f"apt install did not work ({exc}); trying a venv instead.")
    else:
        log_fn("PySide6 is not installed; apt install is not available here.")

    if not system:
        log_fn("No python3 found on this system, so nothing can be installed.")
        return None
    venv_dir = os.path.join(install_dir, "venv")
    log_fn(f"Creating a private Python environment in {venv_dir}...")
    try:
        result = subprocess.run([system, "-m", "venv", venv_dir], timeout=300)
        if result.returncode != 0:
            log_fn("Could not create the Python environment.")
            return None
    except Exception as exc:  # noqa: BLE001
        log_fn(f"Could not create the Python environment ({exc}).")
        return None
    venv_python = os.path.join(venv_dir, "bin", "python")
    log_fn("Downloading PySide6 into the private environment (needs internet)...")
    try:
        result = subprocess.run(
            [venv_python, "-m", "pip", "install", "--quiet", "PySide6"],
            timeout=900)
    except Exception as exc:  # noqa: BLE001
        log_fn(f"PySide6 download failed ({exc}).")
        return None
    if result.returncode != 0 or not python_can_import_pyside6(venv_python):
        log_fn("PySide6 could not be installed into the private environment.")
        return None
    log_fn("PySide6 installed in the private environment.")
    return os.path.abspath(venv_python)


def check_libmtp(log_fn=log) -> bool:
    """True when libmtp is present; warns plainly (never fails) if not."""
    if ctypes.util.find_library("mtp"):
        return True
    ldconfig = shutil.which("ldconfig")
    if ldconfig:
        try:
            out = subprocess.run([ldconfig, "-p"], capture_output=True,
                                 text=True, timeout=30)
            if "libmtp" in out.stdout:
                return True
        except Exception:
            pass
    log_fn("NOTE: libmtp was not found. Connecting a real phone over USB "
           "needs it; folder mode still works. To add it, run: "
           "sudo apt install libmtp9")
    return False


def copy_tree(src: str, install_dir: str, log_fn=log) -> None:
    """Copy the app tree into install_dir (re-runnable).

    A working venv inside the old install dir is preserved across the
    wipe so a reinstall does not throw away its provisioned Python, and
    so are the user's transfer history (history.db*) and logs: they live
    in the same directory by contract, and reinstalling must not wipe
    the app's memory of what was already copied.
    """
    package = os.path.join(src, "android_photo_transfer")
    if not os.path.isdir(package):
        raise FileNotFoundError(
            f"Could not find the app folder: {package}\n"
            "Run the installer from the extracted Android Photo Transfer folder.")
    keep_dir = None
    keep_names = ("venv", "history.db", "history.db-wal", "history.db-shm", "logs")
    kept = [name for name in keep_names
            if os.path.exists(os.path.join(install_dir, name))]
    # A venv that cannot import PySide6 is dead weight (a failed provision
    # can leave ~500 MB behind): drop it instead of shuttling it around.
    # The installer re-provisions when one is needed.
    if "venv" in kept and not python_can_import_pyside6(
            os.path.join(install_dir, "venv", "bin", "python")):
        kept.remove("venv")
    if kept:
        # Keep data BESIDE the install dir (same filesystem), never in /tmp:
        # a venv is hundreds of MB, /tmp may be a small tmpfs, and a
        # cross-device move is slow and strands data if interrupted.
        parent = os.path.dirname(install_dir)
        keep_dir = tempfile.mkdtemp(prefix=".apt-install-keep-", dir=parent)
        for name in kept:
            shutil.move(os.path.join(install_dir, name),
                        os.path.join(keep_dir, name))
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir)
    os.makedirs(install_dir)
    shutil.copytree(package, os.path.join(install_dir, "android_photo_transfer"))
    icons_src = os.path.join(src, "icons")
    if os.path.isdir(icons_src):
        shutil.copytree(icons_src, os.path.join(install_dir, "icons"))
    readme_src = os.path.join(src, "README.md")
    if os.path.isfile(readme_src):
        shutil.copy2(readme_src, os.path.join(install_dir, "README.md"))
    if keep_dir is not None:
        for name in kept:
            shutil.move(os.path.join(keep_dir, name),
                        os.path.join(install_dir, name))
        shutil.rmtree(keep_dir, ignore_errors=True)
        log_fn(f"Kept your existing data across the reinstall: {', '.join(kept)}")
    log_fn(f"Copied the app to {install_dir}")


def write_launcher(install_dir: str, python: str, log_fn=log) -> str:
    """Write the launcher script; returns its path."""
    launcher = os.path.join(install_dir, "android-photo-transfer")
    # The python path goes in double quotes inside the file (safe even
    # with spaces); the .desktop entry quotes the launcher path instead.
    content = (
        "#!/bin/sh\n"
        "# Launcher written by the Android Photo Transfer installer.\n"
        "DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
        "cd \"$DIR\" || exit 1\n"
        f"exec \"{python}\" -m android_photo_transfer \"$@\"\n"
    )
    with open(launcher, "w") as fh:
        fh.write(content)
    os.chmod(launcher, 0o755)
    log_fn(f"Wrote the launcher ({launcher})")
    return launcher


def write_menu_entry(install_dir: str, launcher: str, home: str | None = None,
                     log_fn=log) -> str:
    """Write ~/.local/share/applications/android-photo-transfer.desktop."""
    icon = os.path.join(install_dir, "icons", "app-256.png")
    if not os.path.isfile(icon):
        icon = os.path.join(install_dir, "icons", "app-128.png")
    desktop_dir = os.path.join(home or home_dir(), ".local", "share",
                               "applications")
    os.makedirs(desktop_dir, exist_ok=True)
    entry_path = os.path.join(desktop_dir, APP_ID + ".desktop")
    # Exec is one double-quoted token (GLib rejects nested escaped quotes;
    # the launcher script itself carries the python path, so no nesting).
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Copy photos and videos from an Android phone to this PC\n"
        f"Exec=\"{launcher}\"\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "Keywords=phone;photo;transfer;android;mtp;\n"
        "StartupWMClass=android-photo-transfer\n"
    )
    with open(entry_path, "w") as fh:
        fh.write(content)
    os.chmod(entry_path, 0o644)
    log_fn(f"Added the menu entry ({entry_path})")
    return entry_path


def verify_install(install_dir: str, python: str, log_fn=log) -> bool:
    """Run `<python> -c "import android_photo_transfer"` from install_dir."""
    try:
        result = subprocess.run(
            [python, "-c", "import android_photo_transfer"],
            cwd=install_dir, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        log_fn(f"Could not check the install ({exc}).")
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        log_fn("The installed app could not start.")
        if detail:
            log_fn("The error was: " + detail[-1])
        _name_missing_library(result.stderr or "", log_fn)
        return False
    return True


def _name_missing_library(stderr: str, log_fn) -> None:
    """Name a missing system library plainly instead of dying silently."""
    import re

    match = re.search(r"lib\w+\.so[\w.]*", stderr)
    if match:
        log_fn(f"It looks like a system library is missing: {match.group(0)}")
        log_fn("On Mint, try: sudo apt install python3-pyside6 libmtp9")
    elif "PySide6" in stderr or "No module named" in stderr:
        log_fn("PySide6 is missing for that Python. Try installing it with: "
               "sudo apt install python3-pyside6")


def install(install_dir: str | None = None, home: str | None = None,
            provision: bool = True, write_menu: bool = True,
            log_fn=log) -> int:
    """Run every install step. Returns a process-style exit code."""
    src = source_dir()
    home = home or home_dir()
    install_dir = install_dir or default_install_dir(home)
    log_fn(f"Installing {APP_NAME} to {install_dir}")

    python = find_usable_python(install_dir)
    if python:
        log_fn(f"Found a Python with PySide6: {python}")
    elif provision:
        python = provision_python(install_dir, log_fn)
        if python:
            log_fn(f"Using the new Python: {python}")
    if not python:
        log_fn("FAILED: no Python with PySide6 could be found or installed.")
        log_fn("Install it with: sudo apt install python3-pyside6")
        log_fn("Then run this installer again.")
        return 1

    try:
        copy_tree(src, install_dir, log_fn)
    except FileNotFoundError as exc:
        log_fn(f"FAILED: {exc}")
        return 1
    except OSError as exc:
        log_fn(f"FAILED: could not copy the app files ({exc}).")
        return 1

    # If the usable Python was the old install venv, copy_tree preserved
    # it; if provisioning created one, it is already in place.
    launcher = write_launcher(install_dir, python, log_fn)
    if write_menu:
        try:
            write_menu_entry(install_dir, launcher, home, log_fn)
        except OSError as exc:
            log_fn(f"Warning: could not write the menu entry ({exc}). "
                   f"You can still start the app with {launcher}")
    check_libmtp(log_fn)
    if not verify_install(install_dir, python, log_fn):
        log_fn("FAILED: the installed app did not pass its start-up check.")
        return 1
    log_fn("")
    log_fn(f"Installed. Find '{APP_NAME}' in your applications menu.")
    log_fn(f"Or start it with: {launcher}")
    return 0


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Install Android Photo Transfer.")
    parser.add_argument("--install-dir", default=None,
                        help="install location (default ~/.local/share/android-photo-transfer)")
    parser.add_argument("--home", default=None,
                        help="home directory to install under (for tests)")
    parser.add_argument("--no-provision", action="store_true",
                        help="do not install PySide6; fail if none is usable")
    parser.add_argument("--no-menu", action="store_true",
                        help="do not write the applications-menu entry")
    args = parser.parse_args(argv)
    home = args.home or os.environ.get("HOME") or home_dir()
    if args.home:
        os.environ["HOME"] = args.home
    return install(install_dir=args.install_dir, home=home,
                   provision=not args.no_provision,
                   write_menu=not args.no_menu)


if __name__ == "__main__":
    sys.exit(main())
