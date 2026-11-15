"""libmtp backend: DeviceAdapter over libmtp via ctypes.

Replaces the Windows WPD/COM path (upstream WpdAdapter.cs). libmtp is
loaded lazily with ctypes; if it is absent, list_phones() returns []
and connect() raises DeviceError, so the app still starts and the demo
folder phone still works.

MTP has no path strings: objects are addressed by (storage_id,
object_id). This adapter exposes the WPD-style path model the rest of
the app expects ("Internal storage/DCIM/Camera/IMG_0001.jpg") and
resolves each path to object ids by walking the folder tree, caching
results per connection. persistent_id is the MTP object id as a string
(object ids are unique per device within a session).

Listing uses LIBMTP_Get_Files_And_Folders (one level), which is the
right call for list_folder; LIBMTP_Get_Filelisting_With_Callback lists
the whole device recursively and is not needed here.

All libmtp calls for one adapter are serialized with an RLock (the
Python equivalent of upstream's single MtpWorker thread): libmtp is
not thread-safe per device.
"""
from __future__ import annotations

import ctypes
import io
import logging
import os
import tempfile
import threading
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_int64,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    cast,
    pointer,
)
from datetime import datetime
from typing import BinaryIO, Callable, NamedTuple

from .interface import DeviceAdapter
from .types import (
    DeviceDisconnectedError,
    DeviceEntry,
    DeviceError,
    PhoneInfo,
    PhoneListing,
    StorageRoot,
)

_log = logging.getLogger("android_photo_transfer")

# Names to try for the shared library, in order.
_LIB_NAMES = ("libmtp.so.9", "libmtp.so")

# LIBMTP_filetype_t: folder is the first enum value (0).
LIBMTP_FILETYPE_FOLDER = 0

_LIST_BATCH_SIZE = 200


# ---------------------------------------------------------------------------
# libmtp structs (from libmtp.h, 1.1.x). LIBMTP_mtpdevice_t is opaque to
# callers: programs only ever hold pointers to it, so it is c_void_p here.
# ---------------------------------------------------------------------------

class LIBMTP_device_entry_t(Structure):
    _fields_ = [
        ("vendor", c_char_p),
        ("product", c_char_p),
        ("vendor_id", c_uint16),
        ("product_id", c_uint16),
        ("device_flags", c_uint32),
    ]


class LIBMTP_raw_device_t(Structure):
    _fields_ = [
        ("device_entry", LIBMTP_device_entry_t),
        ("bus_location", c_uint8),
        ("devnum", c_uint8),
    ]


class LIBMTP_file_t(Structure):
    pass


LIBMTP_file_t._fields_ = [
    ("item_id", c_uint32),
    ("parent_id", c_uint32),
    ("storage_id", c_uint32),
    ("filename", c_char_p),
    ("filesize", c_uint64),
    ("modificationdate", c_int64),  # time_t, 64-bit on Linux
    ("filetype", c_int),  # LIBMTP_filetype_t enum; 0 == folder
    ("next", POINTER(LIBMTP_file_t)),
]


class LIBMTP_devicestorage_t(Structure):
    pass


LIBMTP_devicestorage_t._fields_ = [
    ("id", c_uint32),
    ("StorageType", c_uint16),
    ("FilesystemType", c_uint16),
    ("AccessCapability", c_uint16),
    ("MaxCapacity", c_uint64),
    ("FreeSpaceInBytes", c_uint64),
    ("FreeSpaceInObjects", c_uint64),
    ("StorageDescription", c_char_p),
    ("VolumeIdentifier", c_char_p),
    ("next", POINTER(LIBMTP_devicestorage_t)),
    ("prev", POINTER(LIBMTP_devicestorage_t)),
]


# int (*putfunc)(void *priv, void *data, uint32_t *bytes): libmtp calls
# this repeatedly while downloading; return non-zero to abort.
_PutFunc = CFUNCTYPE(c_int, c_void_p, c_void_p, POINTER(c_uint32))
# void (*progressfunc)(uint64_t sent, uint64_t total, void const *data)
_ProgressFunc = CFUNCTYPE(None, c_uint64, c_uint64, c_void_p)


# ---------------------------------------------------------------------------
# Library loading (lazy: importing this module must never fail).
# ---------------------------------------------------------------------------

_lib = None
_lib_tried = False
_init_done = False
_libc = None
_libc_tried = False


def _configure(lib) -> None:
    """Set argtypes/restype on a real CDLL handle.

    Each assignment is guarded: a test double is a plain object whose
    methods do not accept attribute assignment, and that just means
    there is nothing to configure.
    """

    def _set(name: str, restype, argtypes) -> None:
        try:
            fn = getattr(lib, name)
            fn.restype = restype
            fn.argtypes = argtypes
        except (AttributeError, TypeError):
            pass

    _set("LIBMTP_Init", None, [])
    _set(
        "LIBMTP_Detect_Raw_Devices",
        c_int,
        [POINTER(POINTER(LIBMTP_raw_device_t)), POINTER(c_int)],
    )
    _set("LIBMTP_Get_First_Device", c_void_p, [])
    _set(
        "LIBMTP_Open_Raw_Device_Uncached",
        c_void_p,
        [POINTER(LIBMTP_raw_device_t)],
    )
    _set("LIBMTP_Release_Device", None, [c_void_p])
    _set("LIBMTP_Get_Storage", POINTER(LIBMTP_devicestorage_t), [c_void_p, c_int])
    _set(
        "LIBMTP_Get_Files_And_Folders",
        POINTER(LIBMTP_file_t),
        [c_void_p, c_uint32, c_uint32],
    )
    _set(
        "LIBMTP_Get_Folder_List_For_Storage",
        POINTER(LIBMTP_file_t),
        [c_void_p, c_uint32],
    )
    _set(
        "LIBMTP_Get_File_To_File",
        c_int,
        [c_void_p, c_uint32, c_char_p, _ProgressFunc, c_void_p],
    )
    _set(
        "LIBMTP_Get_File_To_Handler",
        c_int,
        [c_void_p, c_uint32, _PutFunc, c_void_p, _ProgressFunc, c_void_p],
    )
    _set(
        "LIBMTP_Get_Thumbnail",
        c_int,
        [c_void_p, c_uint32, POINTER(c_void_p), POINTER(c_uint64)],
    )
    _set("LIBMTP_Delete_Object", c_int, [c_void_p, c_uint32])
    _set("LIBMTP_Destroy_File", None, [POINTER(LIBMTP_file_t)])
    # These return malloc'd strings; restype is c_void_p (not c_char_p)
    # so the pointer survives for string_at + free.
    for name in (
        "LIBMTP_Get_Manufacturername",
        "LIBMTP_Get_Modelname",
        "LIBMTP_Get_Serialnumber",
        "LIBMTP_Get_Friendlyname",
    ):
        _set(name, c_void_p, [c_void_p])


def _load_lib():
    """Try each soname; return the configured handle or None."""
    global _init_done
    for name in _LIB_NAMES:
        try:
            lib = ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
        _configure(lib)
        if not _init_done:
            try:
                lib.LIBMTP_Init()
            except Exception:
                _log.warning("LIBMTP_Init failed", exc_info=True)
            _init_done = True
        return lib
    return None


def _get_lib():
    global _lib, _lib_tried
    if _lib_tried:
        return _lib
    _lib_tried = True
    _lib = _load_lib()
    if _lib is None:
        _log.warning(
            "libmtp not found (tried %s); MTP phones will not be listed",
            ", ".join(_LIB_NAMES),
        )
    return _lib


def _get_libc():
    """libc, for freeing malloc'd libmtp buffers. None if unavailable."""
    global _libc, _libc_tried
    if _libc_tried:
        return _libc
    _libc_tried = True
    try:
        _libc = ctypes.CDLL("libc.so.6", use_errno=True)
        _libc.free.argtypes = [c_void_p]
        _libc.free.restype = None
    except OSError:
        _libc = None
    return _libc


def _free(address) -> None:
    """free() a malloc'd libmtp pointer (int address or None).

    If libc cannot be loaded the bytes leak rather than crash the app.
    """
    if not address:
        return
    libc = _get_libc()
    if libc is None:
        return
    try:
        libc.free(address)
    except Exception:
        pass


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


def _mtime(epoch: int) -> datetime | None:
    if not epoch or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return None


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class _RawDevice(NamedTuple):
    """One detected raw device, as plain Python data."""

    bus_location: int
    devnum: int
    vendor_id: int
    product_id: int
    device_flags: int
    vendor: str | None
    product: str | None


def _detect(lib) -> list[_RawDevice]:
    """Enumerate raw MTP devices as plain data.

    The array libmtp allocates is freed before returning, so every
    field (including the vendor/product strings) is copied out first:
    structs read off a ctypes pointer alias the C memory, they are
    not independent copies, and reading them after the free would be
    use-after-free.
    """
    raw = POINTER(LIBMTP_raw_device_t)()
    count = c_int(0)
    rc = lib.LIBMTP_Detect_Raw_Devices(pointer(raw), pointer(count))
    if rc != 0:
        _log.debug("LIBMTP_Detect_Raw_Devices returned %r", rc)
        return []
    if not raw or count.value <= 0:
        return []
    try:
        entries = []
        for i in range(count.value):
            item = raw[i]
            entry = item.device_entry
            entries.append(
                _RawDevice(
                    bus_location=item.bus_location,
                    devnum=item.devnum,
                    vendor_id=entry.vendor_id,
                    product_id=entry.product_id,
                    device_flags=entry.device_flags,
                    vendor=_decode(entry.vendor),
                    product=_decode(entry.product),
                )
            )
        return entries
    finally:
        _free(cast(raw, c_void_p).value)


def _read_alloc_string(lib, func_name: str, handle) -> str | None:
    """Call a LIBMTP_Get_*name function; decode and free its result."""
    fn = getattr(lib, func_name, None)
    if fn is None:
        return None
    try:
        address = fn(handle)
    except Exception:
        return None
    if not address:
        return None
    try:
        raw = ctypes.string_at(address)
    except Exception:
        raw = None
    finally:
        _free(address)
    text = _decode(raw)
    return text.strip() if text else None


# ---------------------------------------------------------------------------
# Module-level backend API (see interface.py).
# ---------------------------------------------------------------------------

def list_phones() -> list[PhoneListing]:
    """List attached MTP phones. Never raises: [] when libmtp or the
    detection call is unavailable."""
    lib = _get_lib()
    if lib is None:
        return []
    try:
        entries = _detect(lib)
    except Exception as exc:
        _log.warning("libmtp device detection failed: %s", exc)
        return []
    phones = []
    for entry in entries:
        device_id = f"mtp:{entry.bus_location}:{entry.devnum}"
        name = " ".join(
            p for p in (entry.vendor, entry.product) if p
        ).strip()
        phones.append(PhoneListing(device_id, name or "Android device"))
    return phones


def connect(device_id: str) -> "LibmtpAdapter":
    """Open the phone with this device id ("mtp:<bus>:<dev>").

    "mtp:first" opens the first detected device (debugging convenience;
    list_phones never emits it).
    """
    lib = _get_lib()
    if lib is None:
        raise DeviceError(
            "libmtp is not installed, so this phone cannot be opened."
        )
    if device_id == "mtp:first":
        handle = lib.LIBMTP_Get_First_Device()
        if not handle:
            raise DeviceDisconnectedError("No MTP device is connected.")
        return LibmtpAdapter(lib, handle, device_id, bus_dev=None)
    try:
        scheme, bus_s, dev_s = device_id.split(":")
        bus, dev = int(bus_s), int(dev_s)
        if scheme != "mtp":
            raise ValueError
    except ValueError:
        raise DeviceError(f"Not an MTP device id: {device_id!r}")
    entries = _detect(lib)
    match = next(
        (e for e in entries if e.bus_location == bus and e.devnum == dev),
        None,
    )
    if match is None:
        raise DeviceDisconnectedError("The phone is no longer connected.")
    # Rebuild a raw-device struct for the open call: _detect returns
    # plain data because the C array it read from is already freed.
    # The open call uses bus/devnum (plus the id/flags) to find the
    # USB device; the name strings are carried for completeness.
    raw_device = LIBMTP_raw_device_t()
    raw_device.device_entry.vendor = (
        match.vendor.encode("utf-8") if match.vendor else None
    )
    raw_device.device_entry.product = (
        match.product.encode("utf-8") if match.product else None
    )
    raw_device.device_entry.vendor_id = match.vendor_id
    raw_device.device_entry.product_id = match.product_id
    raw_device.device_entry.device_flags = match.device_flags
    raw_device.bus_location = match.bus_location
    raw_device.devnum = match.devnum
    handle = lib.LIBMTP_Open_Raw_Device_Uncached(pointer(raw_device))
    if not handle:
        raise DeviceDisconnectedError(
            "Could not open the phone. Unlock it and make sure USB is "
            "set to File Transfer."
        )
    return LibmtpAdapter(lib, handle, device_id, bus_dev=(bus, dev))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class _TempFileStream(io.BufferedReader):
    """Binary stream backed by a downloaded temp file.

    libmtp can only fetch a whole file (to a file or a callback), so
    open_read downloads to a temp file and hands back a normal stream;
    the temp file is deleted when the stream is closed.
    """

    def __init__(self, path: str):
        self._tmp_path = path
        try:
            super().__init__(io.FileIO(path, "rb"))
        except Exception:
            _unlink_quietly(path)
            raise

    def close(self) -> None:
        try:
            super().close()
        finally:
            _unlink_quietly(self._tmp_path)


class LibmtpAdapter(DeviceAdapter):
    """DeviceAdapter over one open libmtp device handle."""

    def __init__(self, lib, handle, device_id: str, bus_dev):
        self._lib = lib
        self._handle = handle
        self._device_id = device_id
        # (bus, devnum) used to tell "device gone" from other failures.
        # None for "mtp:first": presence then means "any device present".
        self._bus_dev = bus_dev
        self._lock = threading.RLock()
        self._closed = False
        self._connection_lost = False
        # Storage name -> storage id, rebuilt by get_storage_roots.
        self._storages: dict[str, int] = {}
        # Normalised path -> (storage_id, object_id, is_folder, size).
        self._path_cache: dict[str, tuple[int, int, bool, int]] = {}

        friendly = _read_alloc_string(lib, "LIBMTP_Get_Friendlyname", handle)
        model = _read_alloc_string(lib, "LIBMTP_Get_Modelname", handle)
        maker = _read_alloc_string(lib, "LIBMTP_Get_Manufacturername", handle)
        serial = _read_alloc_string(lib, "LIBMTP_Get_Serialnumber", handle)
        name = (
            friendly
            or " ".join(p for p in (maker, model) if p).strip()
            or "Android device"
        )
        # Stable key mirrors upstream WpdAdapter.StableKey.
        stable_key = f"SN:{serial}" if serial else f"ID:{device_id}"
        self._info = PhoneInfo(device_id, name, stable_key)

    @property
    def info(self) -> PhoneInfo:
        return self._info

    @property
    def is_connected(self) -> bool:
        return (
            not self._closed
            and not self._connection_lost
            and self._handle is not None
        )

    # -- internals ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed or self._handle is None:
            raise DeviceDisconnectedError("The phone connection is closed.")
        if self._connection_lost:
            raise DeviceDisconnectedError("The phone was disconnected.")

    def _device_present(self) -> bool:
        try:
            entries = _detect(self._lib)
        except Exception:
            return False
        if self._bus_dev is None:
            return len(entries) > 0
        return any(
            (e.bus_location, e.devnum) == self._bus_dev for e in entries
        )

    def _fail(self, action: str):
        """Turn a failed libmtp call into the right exception.

        libmtp reports most failures the same way, so a failure is a
        disconnect exactly when the device no longer enumerates.
        """
        if self._device_present():
            raise DeviceError(f"Could not {action} on {self._info.name}.")
        self._connection_lost = True
        raise DeviceDisconnectedError(
            f"The phone was disconnected while trying to {action}."
        )

    def _free_file_list(self, first) -> None:
        # LIBMTP_Destroy_File frees one node (and its filename), so walk
        # the list saving next first.
        destroy = getattr(self._lib, "LIBMTP_Destroy_File", None)
        if destroy is None:
            return
        node = first
        while node:
            nxt = node.contents.next
            try:
                destroy(node)
            except Exception:
                pass
            node = nxt

    def _list_children(self, storage_id: int, parent_id: int) -> list[dict]:
        """One level of a folder as plain dicts (copied off the C list)."""
        first = self._lib.LIBMTP_Get_Files_And_Folders(
            self._handle, storage_id, parent_id
        )
        if not first:
            # NULL means "empty folder" or "call failed": tell them
            # apart by checking whether the device is still there.
            if not self._device_present():
                self._connection_lost = True
                raise DeviceDisconnectedError("The phone was disconnected.")
            return []
        children: list[dict] = []
        try:
            node = first
            while node:
                item = node.contents
                filename = _decode(item.filename) or ""
                children.append(
                    {
                        "item_id": item.item_id,
                        "filename": filename,
                        "filesize": item.filesize,
                        "modificationdate": item.modificationdate,
                        "is_folder": item.filetype == LIBMTP_FILETYPE_FOLDER,
                    }
                )
                node = item.next
        finally:
            self._free_file_list(first)
        return children

    def _resolve(self, path: str) -> tuple[int, int, bool, int]:
        """Resolve a device path to (storage_id, object_id, is_folder, size).

        Walks the folder tree by name, caching every level. MTP has no
        path lookup, so this is the slow path; reads prefer the
        persistent_id fast path in _resolve_file.
        """
        key = path.strip().strip("/")
        if not key:
            raise DeviceError(f"Not a phone path: {path!r}")
        cached = self._path_cache.get(key)
        if cached is not None:
            return cached
        parts = key.split("/")
        storage_id = self._storages.get(parts[0])
        if storage_id is None:
            # Storage list may be stale (or never loaded): refresh once.
            self.get_storage_roots()
            storage_id = self._storages.get(parts[0])
            if storage_id is None:
                raise DeviceError(f"Unknown phone storage: {parts[0]!r}")
        if len(parts) == 1:
            result = (storage_id, 0, True, 0)
            self._path_cache[key] = result
            return result
        parent_id = 0
        current = parts[0]
        match = None
        for segment in parts[1:]:
            children = self._list_children(storage_id, parent_id)
            match = next(
                (c for c in children if c["filename"] == segment), None
            )
            if match is None:
                raise DeviceError(f"Not found on the phone: {path!r}")
            parent_id = match["item_id"]
            current = current + "/" + segment
            self._path_cache[current] = (
                storage_id,
                parent_id,
                match["is_folder"],
                0 if match["is_folder"] else match["filesize"],
            )
        assert match is not None
        return (
            storage_id,
            parent_id,
            match["is_folder"],
            0 if match["is_folder"] else match["filesize"],
        )

    def _resolve_file(self, path: str, persistent_id: str | None) -> tuple[int, int | None]:
        """Resolve a file to (object_id, size or None).

        A usable persistent_id (the MTP object id) skips the path walk,
        mirroring upstream ResolveFile; its size is unknown, so callers
        that need a size cap enforce it while downloading instead.
        """
        if persistent_id:
            try:
                return int(persistent_id), None
            except (TypeError, ValueError):
                pass  # stale or foreign id: fall through to the path
        _storage_id, object_id, is_folder, size = self._resolve(path)
        if is_folder:
            raise DeviceError(f"Not a file on the phone: {path!r}")
        return object_id, size

    # -- DeviceAdapter -----------------------------------------------------

    def get_storage_roots(self) -> list[StorageRoot]:
        with self._lock:
            self._ensure_open()
            first = self._lib.LIBMTP_Get_Storage(self._handle, 0)
            if not first:
                self._fail("list storage")
            roots: list[StorageRoot] = []
            self._storages = {}
            used_names: set[str] = set()
            node = first
            while node:
                item = node.contents
                name = _decode(item.StorageDescription) or f"Storage {item.id}"
                if name in used_names:
                    name = f"{name} ({item.id})"
                used_names.add(name)
                self._storages[name] = item.id
                self._path_cache[name] = (item.id, 0, True, 0)
                roots.append(
                    StorageRoot(
                        path=name,
                        name=name,
                        total_size=item.MaxCapacity or None,
                        free_space=item.FreeSpaceInBytes or None,
                    )
                )
                node = item.next
            # NOTE: libmtp has no public call to free a storage list;
            # the handful of nodes leak until the process exits.
            return roots

    def folder_has_entries(self, path: str) -> bool:
        with self._lock:
            self._ensure_open()
            storage_id, parent_id, is_folder, _size = self._resolve(path)
            if not is_folder:
                raise DeviceError(f"Not a folder on the phone: {path!r}")
            return len(self._list_children(storage_id, parent_id)) > 0

    def list_folder(
        self, path: str, on_batch: Callable[[list[DeviceEntry]], None]
    ) -> None:
        with self._lock:
            self._ensure_open()
            storage_id, parent_id, is_folder, _size = self._resolve(path)
            if not is_folder:
                raise DeviceError(f"Not a folder on the phone: {path!r}")
            children = self._list_children(storage_id, parent_id)
            base = path.strip().strip("/")
            batch: list[DeviceEntry] = []
            for child in children:
                batch.append(
                    DeviceEntry(
                        full_name=f"{base}/{child['filename']}",
                        name=child["filename"],
                        is_folder=child["is_folder"],
                        # MTP has no hidden attribute; dot-files are
                        # hidden by convention.
                        is_hidden=child["filename"].startswith("."),
                        size=0 if child["is_folder"] else child["filesize"],
                        created=None,  # MTP has no creation time
                        modified=_mtime(child["modificationdate"]),
                        persistent_id=(
                            None
                            if child["is_folder"]
                            else str(child["item_id"])
                        ),
                    )
                )
                if len(batch) >= _LIST_BATCH_SIZE:
                    on_batch(batch)
                    batch = []
            if batch:
                on_batch(batch)

    def open_read(self, path: str, persistent_id: str | None) -> BinaryIO:
        with self._lock:
            self._ensure_open()
            object_id, _size = self._resolve_file(path, persistent_id)
            fd, tmp_path = tempfile.mkstemp(prefix="apt-mtp-", suffix=".bin")
            os.close(fd)
            try:
                rc = self._lib.LIBMTP_Get_File_To_File(
                    self._handle, object_id, os.fsencode(tmp_path), None, None
                )
            except Exception:
                _unlink_quietly(tmp_path)
                raise
            if rc != 0:
                _unlink_quietly(tmp_path)
                self._fail(f"read {path!r}")
            return _TempFileStream(tmp_path)

    def get_thumbnail(self, path: str, persistent_id: str | None) -> bytes | None:
        with self._lock:
            self._ensure_open()
            try:
                object_id, _size = self._resolve_file(path, persistent_id)
            except DeviceError:
                return None
            data = c_void_p()
            size = c_uint64(0)
            rc = self._lib.LIBMTP_Get_Thumbnail(
                self._handle, object_id, pointer(data), pointer(size)
            )
            if rc != 0 or not data.value or size.value == 0:
                # Many objects simply have no thumbnail.
                return None
            try:
                return ctypes.string_at(data.value, size.value)
            finally:
                _free(data.value)

    def read_all_bytes(
        self, path: str, persistent_id: str | None, max_bytes: int
    ) -> bytes:
        with self._lock:
            self._ensure_open()
            object_id, size = self._resolve_file(path, persistent_id)
            if size is not None and size > max_bytes:
                raise ValueError(
                    f"File is too large to load for preview ({size} bytes)."
                )
            buffer = io.BytesIO()
            too_big: list[bool] = []

            def _put(priv, data, n_ptr):
                n = n_ptr.contents.value
                if n:
                    buffer.write(ctypes.string_at(data, n))
                if buffer.tell() > max_bytes:
                    # Size was unknown (persistent_id path): abort the
                    # transfer by returning non-zero.
                    too_big.append(True)
                    return 1
                return 0

            putfunc = _PutFunc(_put)
            rc = self._lib.LIBMTP_Get_File_To_Handler(
                self._handle, object_id, putfunc, None, None, None
            )
            if too_big:
                raise ValueError(
                    "File is too large to load for preview "
                    f"(over {max_bytes} bytes)."
                )
            if rc != 0:
                self._fail(f"read {path!r}")
            return buffer.getvalue()

    def delete_file(self, path: str) -> None:
        with self._lock:
            self._ensure_open()
            _storage_id, object_id, _is_folder, _size = self._resolve(path)
            rc = self._lib.LIBMTP_Delete_Object(self._handle, object_id)
            if rc != 0:
                self._fail(f"delete {path!r}")
            key = path.strip().strip("/")
            stale = [
                k
                for k in self._path_cache
                if k == key or k.startswith(key + "/")
            ]
            for k in stale:
                del self._path_cache[k]

    def is_still_connected(self) -> bool:
        if self._closed or self._connection_lost or self._handle is None:
            return False
        with self._lock:
            try:
                return self._device_present()
            except Exception:
                return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    self._lib.LIBMTP_Release_Device(handle)
                except Exception:
                    pass


__all__ = [
    "LIBMTP_FILETYPE_FOLDER",
    "LIBMTP_devicestorage_t",
    "LIBMTP_file_t",
    "LIBMTP_raw_device_t",
    "LibmtpAdapter",
    "connect",
    "list_phones",
]
