"""Shared value types for the Android Photo Transfer Linux port.

Mirrors the C# types in upstream Core/Wpd (PhoneListing, PhoneInfo,
StorageRoot, DeviceEntry) and Core/Devices (ConnectionState).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ConnectionState(Enum):
    WAITING = "waiting"
    CONNECTING = "connecting"
    READY = "ready"
    LOCKED = "locked"
    ERROR = "error"


class DeviceDisconnectedError(Exception):
    """The phone went away mid-operation (unplugged, session reset)."""


class DeviceError(Exception):
    """A device call failed for a reason that is not a disconnect."""


@dataclass(frozen=True)
class PhoneListing:
    device_id: str
    name: str


@dataclass(frozen=True)
class PhoneInfo:
    device_id: str
    name: str
    stable_key: str


@dataclass(frozen=True)
class StorageRoot:
    path: str
    name: str
    total_size: int | None = None
    free_space: int | None = None


@dataclass(frozen=True)
class DeviceEntry:
    full_name: str
    name: str
    is_folder: bool
    is_hidden: bool
    size: int
    created: datetime | None = None
    modified: datetime | None = None
    persistent_id: str | None = None
