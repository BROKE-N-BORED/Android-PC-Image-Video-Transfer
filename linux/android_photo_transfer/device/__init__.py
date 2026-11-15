"""Device layer: backends and manager for the Linux port.

Backends implement DeviceAdapter (see interface.py): libmtp for real
phones, a plain local folder for the demo phone. The package-level
list_phones()/connect() helpers aggregate both backends; DeviceManager
uses the backend modules directly.
"""
from __future__ import annotations

import os

from . import folder_adapter, libmtp_backend
from .folder_adapter import FolderAdapter
from .interface import DeviceAdapter
from .libmtp_backend import LibmtpAdapter
from .manager import DeviceManager
from .types import (
    ConnectionState,
    DeviceDisconnectedError,
    DeviceEntry,
    DeviceError,
    PhoneInfo,
    PhoneListing,
    StorageRoot,
)

__all__ = [
    "ConnectionState",
    "DeviceAdapter",
    "DeviceDisconnectedError",
    "DeviceEntry",
    "DeviceError",
    "DeviceManager",
    "FolderAdapter",
    "LibmtpAdapter",
    "PhoneInfo",
    "PhoneListing",
    "StorageRoot",
    "connect",
    "folder_adapter",
    "libmtp_backend",
    "list_phones",
]


def list_phones() -> list[PhoneListing]:
    """All phones: MTP devices plus the demo folder phone, if set."""
    phones = list(libmtp_backend.list_phones())
    demo_folder = os.environ.get("APT_DEMO_FOLDER")
    if demo_folder and os.path.isdir(demo_folder):
        phones.extend(folder_adapter.list_phones(demo_folder))
    return phones


def connect(device_id: str) -> DeviceAdapter:
    """Open a phone by device id, dispatching on the "demo:" prefix."""
    if device_id.startswith(folder_adapter.DEVICE_ID_PREFIX):
        return folder_adapter.connect(device_id)
    return libmtp_backend.connect(device_id)
