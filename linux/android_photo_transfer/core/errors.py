"""Plain-language error messages for the Android Photo Transfer Linux port.

Ports upstream Infrastructure/ErrorMessages.cs. Linux adaptations:
COMException / FACILITY_WPD checks have no Linux equivalent (DeviceError
from the device layer plays that role), and the permission message says
"the app" instead of "Windows". No em-dashes in user-visible strings.
"""
from __future__ import annotations

import errno

from ..device.types import DeviceDisconnectedError, DeviceError

PHONESTOPPED = (
    "The phone stopped responding. Unlock your phone and make sure USB "
    "is still set to File Transfer."
)

PHONE_STOPPED_RESUME = (
    "The phone stopped responding. Unplug it, plug it back in and unlock "
    "it, then click Resume."
)


class InsufficientSpaceError(Exception):
    """Not enough free space on the PC for the planned transfer."""

    def __init__(self, required: int, available: int):
        super().__init__(
            f"Not enough free space: {required} bytes needed, {available} available."
        )
        self.required = required
        self.available = available


class TransferVerificationError(Exception):
    """A copied file did not match the size the phone reported."""


def format_bytes(count: int) -> str:
    """Human-readable byte count, ported from FileUtilities.FormatBytes."""
    if count < 1024:
        return f"{count} B"
    value = float(count)
    units = ("KB", "MB", "GB", "TB")
    unit = -1
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if value >= 100:
        return f"{value:.0f} {units[unit]}"
    return f"{value:.1f} {units[unit]}"


def is_disk_full(exc: Exception) -> bool:
    """True when the exception means the destination drive ran out of space."""
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)


def is_destination_fatal(exc: Exception) -> bool:
    """Destination-side problems that will fail every remaining file.

    Mirrors ErrorMessages.IsDestinationFatal: disk full, permission denied,
    or the destination folder being gone. When one of these hits, the
    transfer run stops instead of failing file after file.
    """
    return (
        is_disk_full(exc)
        or isinstance(exc, PermissionError)
        or isinstance(exc, FileNotFoundError)
    )


def describe(exc: Exception) -> str:
    """Translate an exception into a message a normal person can act on.

    Technical detail goes to the log; this is what the user sees.
    """
    if isinstance(exc, InsufficientSpaceError):
        return (
            "Not enough free space on the PC. Needed: "
            f"{format_bytes(exc.required)}, available: {format_bytes(exc.available)}."
        )
    if isinstance(exc, DeviceDisconnectedError):
        return (
            "The phone was disconnected. Reconnect it and make sure USB "
            "is set to File Transfer."
        )
    if isinstance(exc, TransferVerificationError):
        return "The copied file was incomplete, so it was discarded. Try again."
    if isinstance(exc, PermissionError):
        return (
            "The app was not allowed to save to that folder. "
            "Choose a different destination folder."
        )
    if isinstance(exc, FileNotFoundError):
        return (
            "The destination folder is no longer available. If it is on an "
            "external drive, reconnect it or choose another folder."
        )
    if is_disk_full(exc):
        return (
            "The PC drive is full. Free up space or choose another "
            "destination folder."
        )
    if isinstance(exc, (TimeoutError, DeviceError)):
        return PHONESTOPPED
    return f"Something went wrong: {exc}"
