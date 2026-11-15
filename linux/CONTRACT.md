# Linux port — shared contract (READ FIRST, all builders)

Package: `android_photo_transfer`. Entry: `python3 -m android_photo_transfer`
(`android_photo_transfer/__main__.py` + `main.py`). Python 3.10+, PySide6,
stdlib sqlite3 only. No other third-party deps. Hard $0. No secrets in code.

Upstream C# source (read it, port the LOGIC): `../upstream/PROJECT ROOT/AndroidPhotoTransfer/`
Audit: `~/workspace/github-audit/android-pc-image-video-transfer-deep-audit-2026-11-12.md`
Design rules: `~/workspace/design-rules.md` (toy test, cheap motion only,
select NOTHING by default, no bare `&` in visible labels, no em-dashes in
visible UI text). AGENTS.md Qt/zip/installer lessons apply.

## Paths (XDG, Linux)
- Settings JSON: `~/.config/android-photo-transfer/settings.json` (atomic save: tmp + os.replace)
- History SQLite: `~/.local/share/android-photo-transfer/history.db` (WAL mode)
- Logs: `~/.local/share/android-photo-transfer/logs/` with rollover
- Default destination: `~/Pictures/Phone Photos`

## device/types.py (WRITTEN — import it, do not redefine)
- `PhoneListing(device_id: str, name: str)` dataclass
- `PhoneInfo(device_id: str, name: str, stable_key: str)` dataclass
- `StorageRoot(path: str, name: str, total_size: int | None, free_space: int | None)` dataclass
- `DeviceEntry(full_name, name, is_folder: bool, is_hidden: bool, size: int, created: datetime | None, modified: datetime | None, persistent_id: str | None)` dataclass
- `ConnectionState` enum: WAITING, CONNECTING, READY, LOCKED, ERROR
- `DeviceDisconnectedError(Exception)`, `DeviceError(Exception)`

## device/interface.py (WRITTEN — implement it, do not change signatures)
`DeviceAdapter` ABC, all methods BLOCKING, called from a worker thread:
- `info: PhoneInfo` (property), `is_connected: bool` (property)
- `get_storage_roots() -> list[StorageRoot]`
- `folder_has_entries(path: str) -> bool`
- `list_folder(path: str, on_batch: Callable[[list[DeviceEntry]], None]) -> None`
  (calls on_batch in batches of <= 200 entries)
- `open_read(path: str, persistent_id: str | None) -> BinaryIO`
- `get_thumbnail(path: str, persistent_id: str | None) -> bytes | None`
- `read_all_bytes(path: str, persistent_id: str | None, max_bytes: int) -> bytes`
  (raise ValueError if file is larger than max_bytes)
- `delete_file(path: str) -> None`
- `is_still_connected() -> bool`
- `close() -> None`
- `list_phones() -> list[PhoneListing]` (module-level per backend) and
  `connect(device_id: str) -> DeviceAdapter` (module-level per backend).
  Raise `DeviceDisconnectedError` when the device is gone / connection lost.

## device/manager.py (device layer owns it)
`DeviceManager(on_changed: Callable[[], None])`:
- properties: `state: ConnectionState`, `phones: list[PhoneListing]`,
  `current: DeviceAdapter | None`, `storage_roots: list[StorageRoot]`,
  `error_message: str | None`
- `refresh() -> None` (blocking; UI calls it from a worker thread):
  list phones via libmtp backend; if env `APT_DEMO_FOLDER` names an existing
  dir (read at DeviceManager CONSTRUCTION time, not per refresh), append a
  folder-adapter phone `("demo:<abspath>", "Demo Phone (folder)")`;
  drop a current adapter whose device vanished or `is_connected` is False;
  auto-connect to preferred phone or first phone; after connect, list storage
  roots and probe `folder_has_entries` on each root: any readable root ->
  READY, else LOCKED ("unlock it and choose File Transfer") with a 3 s retry
  timer (threading.Timer, reschedules itself while LOCKED/ERROR).
  LOCKED keeps `error_message` None (the locked wording comes from state +
  device name; error_message is for ERROR state only).
  On exception: log, set ERROR + `error_message = errors.describe(exc)`,
  schedule retry. Always call `on_changed()` at the end.
- `reconnect() -> None`, `select_phone(device_id: str) -> None`, `shutdown() -> None`
- Connect timeout: 30 s (run connect in a thread, join with timeout).

## core/classifier.py
- `PHOTO_EXTS`: 20 (jpg jpeg png gif bmp webp avif heic heif tif tiff dng arw cr2 cr3 nef orf rw2 + jxl? NO — match MediaClassifier.cs exactly; read it)
- `VIDEO_EXTS`: 12 (mp4 mov m4v 3gp 3g2 mkv avi webm wmv mts m2ts ts — upstream has 12, not 13)
- `is_photo(name)`, `is_video(name) -> bool` (extension match, case-insensitive)
- `file_category(name) -> str`: "Photos" | "Videos" | "Documents" | "Audio" | "Archives" | "Apps" | "Other" (match MediaClassifier categories)
- `folder_category(phone_path: str) -> str`: "Camera" | "Screenshots" | "Downloads" | "Pictures" | "Other" (match MediaClassifier)

## core/scanner.py
- `MediaItem` dataclass: `source_path: str` (device path), `name: str`,
  `size: int`, `modified: datetime | None`, `created: datetime | None`,
  `folder: str` (phone folder path), `folder_category: str`, `kind: str`
  ("photo" | "video" | "file"), `file_category: str`,
  `persistent_id: str | None`, `device_key: str`,
  `already_transferred: bool = False`
- `scan(adapter, device_key: str, on_batch: Callable[[list[MediaItem]], None], should_stop: Callable[[], bool]) -> None`:
  breadth-first walk of every storage root; priority folders first
  (DCIM, Pictures, Download, Movies, Documents); skip hidden/dot-folders
  ONLY — Android/data and Android/obb are INCLUDED (Joshua's v1.0.4 order);
  unreadable folders logged + skipped, never fatal; stream results in
  batches via on_batch. Classify each file with classifier.py.
- `MediaQuery`: `apply(items, show: str, search: str, sort_key: str, sort_desc: bool) -> list[MediaItem]`
  show in {"all", "not_on_pc", "on_pc"}; sort_key in {"name","size","date","folder"}.

## core/destination.py
- `resolve(dest_root: str, item: MediaItem, organize_by_folder: bool, skip_existing: bool = True) -> str`:
  never-overwrite naming (`name_1.jpg`, `name_2.jpg`, ...); if
  organize_by_folder, mirror the phone folder under dest_root (sanitize
  path segments, block `..` escapes). With skip_existing on, an existing
  same-name same-size file is returned as-is (caller skips it); with it
  off, the name is stepped to `name_1`.
- `is_already_there(path: str, size: int) -> bool`: same name + same size
  (name+size dedupe, matching upstream; NO hash in this build).

## core/transfer.py
- `TransferItem` dataclass: `item: MediaItem`, `status: str`
  ("pending" | "copying" | "done" | "skipped" | "failed" | "cancelled"),
  `error: str | None = None`
- `TransferManager(history, adapter=None)` with `run(transfer_items: list[TransferItem], dest_root: str, skip_existing: bool, organize_by_folder: bool, progress: TransferProgress, should_pause: Callable[[], bool], should_cancel: Callable[[], bool], adapter=None) -> TransferResult`:
  the adapter comes from the constructor or the `run(adapter=...)` keyword
  (the run keyword wins); `run()` raises ValueError before copying anything
  if neither is set. Callers (UI) MUST pass the current device adapter.
  one file at a time; 1 MB buffered copy; write to `name.partial`, verify
  byte count, atomic `os.replace`; never overwrite (via destination.py);
  skip identical name+size when skip_existing; free-space pre-check with
  50 MB margin (raise InsufficientSpaceError naming required/available);
  pause: check should_pause between chunks, current file restarts cleanly
  on resume; transient read errors retry ONCE after 0.5 s; 5 consecutive
  failures stop the run, rest stay "pending" for Resume; disconnect
  (DeviceDisconnectedError) rolls current file back to "pending" and the
  run ends with `disconnected=True`; preserve original mtime/atime on the
  copied file; record each success in history (history failure only logs).
- `TransferProgress`: simple object with `copied, skipped, failed: int`,
  `current_name: str`, `current_bytes, current_total: int`,
  `overall_done, overall_total: int`, and an `on_change: Callable` the
  caller sets; manager calls it after every chunk batch / item finish.
- `TransferResult` dataclass: `disconnected: bool`, `stopped_early: bool`.
- `InsufficientSpaceError(Exception)` with `required`, `available` attrs.

## core/history.py
- `HistoryDatabase(path: str | None = None)` (default XDG path above);
  sqlite3, WAL mode, table `transfers(device_key TEXT, source_path TEXT, source_size INTEGER, PRIMARY KEY(device_key, source_path, source_size))`.
- `record(device_key, source_path, size) -> None`,
  `get_transferred_keys(device_key) -> set[str]` (keys `"path|size"`),
  `close() -> None`. All failures raise; CALLERS catch + log (never fatal).

## core/settings.py
- `Settings` dataclass: `dest_folder: str` (default `~/Pictures/Phone Photos`),
  `skip_existing: bool = True`, `open_folder_after_transfer: bool = False`,
  `organize_by_folder: bool = False`, `thumbnails_enabled: bool = True`,
  `thumbnail_cache_mb: int = 64` (clamp 16..1024),
  `window_geometry: dict | None = None` (`{"x","y","w","h"}`).
- `SettingsManager(path: str | None = None)`: `.current: Settings`,
  `.save() -> None` (atomic tmp + os.replace), loads with defaults on
  missing/corrupt file.

## core/errors.py
- `describe(exc: Exception) -> str`: plain-language messages mirroring
  ErrorMessages.cs (DeviceDisconnectedError -> "The phone was disconnected.
  Reconnect it and make sure USB is set to File Transfer.";
  InsufficientSpaceError -> free-space message; COMException n/a on Linux;
  TimeoutError -> PhoneStoppedResponding text; fallback `f"Something went
  wrong: {exc}"`). `PHONESTOPPED = "The phone stopped responding. Unlock your
  phone and make sure USB is still set to File Transfer."`
- `is_destination_fatal(exc) -> bool`: disk full / permission / missing dir.

## core/logutil.py
- `setup_logging() -> logging.Logger`: file logs under the XDG logs dir,
  RotatingFileHandler (1 MB x 3), plus `log_exception` helper. Name the
  logger "android_photo_transfer".

## ui/ (PySide6)
- Main window 1220x780, minimum 960x580. Two tabs: "Photos and Videos"
  (thumbnail grid 160x186 tiles + list view toggle) and "Files" (file-type
  filter + Quick Access + folder tree + sortable details list).
- Device state drives a status bar + waiting/locked panels mirroring
  MainViewModel.cs strings ("No phone connected",
  "Waiting for a phone — connect it with a USB cable" — NOTE: no em-dash in
  visible text, use a colon or comma: "Waiting for a phone: connect it with
  a USB cable"). Locked: "<name> detected: unlock it and choose File Transfer".
- Selection: click, Shift+click range, Ctrl+A; Transfer Selected /
  Transfer All / Import New; Show filter (All / Not on PC yet / Already on
  PC); search box; sort. Transfer overlay: overall + per-file progress,
  Copied/Skipped/Failed counts, Pause, Cancel, Retry failed, Resume.
- Delete from Phone WITH SAFETY FIX (do NOT port the upstream hole):
  default deletes ONLY items already on the PC; items not yet copied are
  excluded and named in the dialog; deleting untransferred items needs a
  SECOND explicit confirmation (checkbox + separate confirm dialog).
- Preview window: photo preview with size cap (match upstream cap; if the
  cap value is unreadable in upstream use 50 MB and note it), prev/next,
  transfer-from-preview; video shows "Video preview is not available.
  Transfer the video to play it on this PC."
- Thumbnails: device thumbnail first; photo full-decode fallback for files
  <= 30 MB; LRU memory cache, default 64 MB (clamp 16..1024).
- Settings window: destination folder, skip-existing, open-folder-after,
  organize-by-folder, thumbnails on/off, cache size, Open Logs button.
- Close-during-transfer guard (confirm dialog). Window placement persisted
  + validated against the screen (fall back to centered).
- All device/transfer work on QThreads; UI updates via signals only.
- `main.py`: QApplication setup, wiring (settings, history with graceful
  degradation, device manager, thumbnail cache, view models), global
  exception handler with a friendly dialog, shutdown (dispose + save).
- NO bare `&` in any visible label. NO em-dashes in visible UI text.
  Select nothing by default. Cheap motion only.

## tests/ (unittest, stdlib only)
- `fakes.py`: FakeAdapter implementing DeviceAdapter over an in-memory
  file tree (mirror upstream FakePhone in UnitTest1.cs), FakeStream,
  TempFolder, TestData helpers.
- Engine tests mirror upstream TransferManagerTests/CoreTests: byte-for-byte
  copy, no .partial leftovers, dedupe, retry, pause/resume,
  disconnect/resume, folder mirroring, 100% progress incl. skipped,
  scanner (finds photos/videos incl. Android/data, skips hidden folders),
  categories, sort/filter/search, history per-device, log rollover,
  filename sanitizing, path-escape prevention, settings round-trip,
  delete-safety (untransferred blocked by default).
- Qt GUI tests run with `xvfb-run -a` + `LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu`,
  default HOME (NEVER override HOME for Qt/xcb runs), `TMPDIR=/tmp`.
  Qt test python: `~/workspace/forge-v2-testenv/bin/python` (has PySide6 6.12.0).

## Out of scope (do NOT build)
Hash dedupe, auto-import on connect, video preview/playback, HEIC-to-JPEG
conversion, date-based folders, queue persistence. Delete safety fix IS in.
