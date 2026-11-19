# Android Photo Transfer (Linux port)

Copy photos and videos from an Android phone to a Linux PC over USB.
Python 3 + PySide6 (Qt). A port of the Windows app
(AndroidPhotoTransfer, C#/WPF) to Linux Mint.

## Requirements

- Linux Mint (or any Debian-based Linux), 64-bit
- Python 3.10 or newer
- PySide6 (the installer installs it if it is missing)
- libmtp9 for connecting a real phone over USB (`sudo apt install libmtp9`).
  Without it the app still runs and folder mode still works.
- About 100 MB of disk for the app plus room for your photos

## Install (no terminal needed)

1. Download the release zip and open it in the file manager.
2. Extract it anywhere (for example your home folder).
3. Double-click **Install Android Photo Transfer**.
4. Find **Android Photo Transfer** in your applications menu.

The installer copies the app to `~/.local/share/android-photo-transfer`,
adds the menu entry, and checks the installed app actually starts before
it reports success. Running it again reinstalls cleanly.

Terminal install (if you prefer it):

```sh
bash install.sh
```

Use `bash install.sh`, never `sh install.sh`.

## Run

From the applications menu, or:

```sh
~/.local/share/android-photo-transfer/android-photo-transfer
```

To try the app with no phone, point it at a folder of photos instead:

```sh
APT_DEMO_FOLDER=/path/to/a/folder/with/photos android-photo-transfer
```

That folder shows up as "Demo Phone (folder)".

## What it does

- Lists the photos and videos on the phone (Photos and Videos tab) and
  every file (Files tab), including Android/data folders.
- Copies them to `~/Pictures/Phone Photos` (changeable in Settings),
  optionally mirroring the phone's folders.
- Skips files already on the PC (same name and size); never overwrites a
  different file with the same name (it becomes `name_1.jpg`).
- Copies one file at a time, verifies the size, and never leaves a
  half-copied file behind. A failed read retries once; an unplugged phone
  pauses the run and Resume finishes it after reconnecting.
- Remembers what was copied, per phone, so "Not on PC yet" works.
- Delete from Phone only deletes items already on the PC by default;
  deleting anything not yet copied needs a second confirmation.

## Updates

The app checks for a new version once when it starts (turn it off in
Settings, or in the Help menu: "Check for updates on startup"). You can
also check any time with Help > Check for Updates.

When a new version is out, the app asks whether to update. Choosing
Update downloads the new release, checks it against its checksum, and
installs it; your photos, settings and transfer history stay where they
are. Afterwards it asks whether to restart now: say yes and the app
restarts into the new version, say no and the update takes effect the
next time you open the app.

If you run the app from source instead of installing it, it cannot
update itself: the update dialog opens the release page so you can get
the new version there.

## Build from source

```sh
git clone <repo-url>
cd android-photo-transfer-linux-port/port
python3 -m android_photo_transfer
```

Needs PySide6: `sudo apt install python3-pyside6` (Mint).

Build the release zip:

```sh
python3 build_zip.py --version 0.1.1
```

The zip lands in `dist/` and self-checks its file modes (scripts stay
executable when extracted with a GUI archiver). `build_zip.py` also
writes a `.sha256` sidecar next to the zip; a release must publish the
zip and its sidecar together, or the in-app updater will never offer it
(it refuses any release without a checksum file).

## Tests

```sh
python3 tests/run_tests.py          # engine tests (no Qt needed)
python3 tests/run_tests.py --gui    # + Qt GUI smoke tests (needs Xvfb)
```

Tests use an in-memory fake phone (`tests/fakes.py`); no phone needed.
See `CONTRACT.md` for the module contract the tests are written against.

## Honest limits

- **Real-device transfer is not verified yet.** The libmtp backend needs
  a test on a real PC with a real phone plugged in (the sandbox has no
  phone and no libmtp). Folder mode and the fake-phone suite are tested.
- **Android 11 and newer do not expose Android/data over MTP.** The app
  still scans Android/data when the phone offers it (older phones, some
  apps' folders), but on a modern phone those folders may simply be
  absent from the listing. That is an Android restriction, not a bug.
- No hash dedupe (name + size only), no auto-import on connect, no video
  preview or playback, no HEIC-to-JPEG conversion, no date-based folders.
- A phone that is locked, or whose USB mode is not File Transfer, shows
  as locked: unlock it and choose File Transfer on the phone.

## Files

- `android_photo_transfer/` — the app (device, core, ui)
- `tests/` — unittest suite + fake phone
- `install.sh`, `install_gui.py`, `install_lib.py` — installers
- `build_zip.py` — release zip builder
- `icons/` — app icon (SVG source + rendered PNGs)
- `CONTRACT.md` — the shared contract between the app's parts
