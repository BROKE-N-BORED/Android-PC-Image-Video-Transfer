"""Device adapter interface for the Android Photo Transfer Linux port.

Mirrors upstream IWpdAdapter (Core/Wpd/IWpdAdapter.cs). Every method is
BLOCKING and is always called from a worker thread, never the UI thread.
Backends: libmtp (ctypes) for real phones, folder adapter for local
folders / gvfs MTP mounts / testing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO, Callable

from .types import DeviceEntry, PhoneInfo, StorageRoot


class DeviceAdapter(ABC):
    """One connected phone (or phone-like folder)."""

    @property
    @abstractmethod
    def info(self) -> PhoneInfo:
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def get_storage_roots(self) -> list[StorageRoot]:
        ...

    @abstractmethod
    def folder_has_entries(self, path: str) -> bool:
        ...

    @abstractmethod
    def list_folder(
        self, path: str, on_batch: Callable[[list[DeviceEntry]], None]
    ) -> None:
        """List one folder, calling on_batch with batches of <= 200 entries."""

    @abstractmethod
    def open_read(self, path: str, persistent_id: str | None) -> BinaryIO:
        ...

    @abstractmethod
    def get_thumbnail(self, path: str, persistent_id: str | None) -> bytes | None:
        ...

    @abstractmethod
    def read_all_bytes(
        self, path: str, persistent_id: str | None, max_bytes: int
    ) -> bytes:
        """Read a whole file; raise ValueError if it is larger than max_bytes."""

    @abstractmethod
    def delete_file(self, path: str) -> None:
        ...

    @abstractmethod
    def is_still_connected(self) -> bool:
        ...

    @abstractmethod
    def close(self) -> None:
        ...
