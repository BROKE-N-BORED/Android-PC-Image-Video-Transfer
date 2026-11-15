"""Destination picking for the Android Photo Transfer Linux port.

Ports upstream Core/Transfer/DestinationResolver.cs and the
SanitizeFileName / SanitizeRelativeFolder helpers from
Infrastructure/FileUtilities.cs.

Never overwrites: a different file with the same name becomes
IMG_1001_1.jpg, IMG_1001_2.jpg, ... A file with the same name and exact
size is treated as already transferred (skipped) when skip_existing is
on. Dedupe is name + size only; no hashing in this build.
"""
from __future__ import annotations

import os
import uuid

from .scanner import MediaItem

_MAX_NUMBERED = 100000


def sanitize_file_name(name: str) -> str:
    """Make a phone file name safe for the local filesystem.

    Phones allow characters a PC filesystem does not (':', '?', ...).
    Replaces the Windows-invalid set (< > : " / \\ | ? *), NUL and control
    characters with '_' (the same set upstream uses, so copied files also
    stay valid on FAT/exFAT sticks and Windows shares. Trims whitespace
    and trailing dots (upstream parity), and never returns an empty name.
    """
    invalid = set('<>:"/\\|?*')
    cleaned = "".join(
        "_" if c in invalid or c == "\x00" or ord(c) < 0x20 else c
        for c in name
    )
    result = cleaned.strip().rstrip(".")
    return result if result else "unnamed"


def sanitize_relative_folder(folder: str) -> str:
    """Turn a phone folder such as "DCIM/Camera" into a safe relative path.

    Drops empty segments and "." / ".." segments, so a hostile or odd
    folder name can never escape the destination root.
    """
    parts = [
        sanitize_file_name(part)
        for part in folder.replace("\\", "/").split("/")
        if part and part != "." and part != ".."
    ]
    return os.path.join(*parts) if parts else ""


def _is_within(root: str, path: str) -> bool:
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    return os.path.commonpath([root_abs]) == os.path.commonpath([root_abs, path_abs])


def destination_folder_for(
    dest_root: str, item: MediaItem, organize_by_folder: bool
) -> str:
    """The folder a file lands in: dest_root, or dest_root/<phone folder>."""
    if not organize_by_folder or not item.folder:
        return dest_root
    folder = os.path.join(dest_root, sanitize_relative_folder(item.folder))
    if not _is_within(dest_root, folder):
        return dest_root
    return folder


def resolve(
    dest_root: str,
    item: MediaItem,
    organize_by_folder: bool,
    skip_existing: bool = True,
) -> str:
    """Pick the destination path for one item; never returns an overwrite.

    With skip_existing on (the default), an existing file with the same
    name and exact size is returned as-is: the caller recognises it with
    is_already_there() and skips the copy. With skip_existing off, every
    existing candidate is stepped past (name_1, name_2, ...).
    """
    folder = destination_folder_for(dest_root, item, organize_by_folder)
    safe_name = sanitize_file_name(item.name)
    stem, extension = os.path.splitext(safe_name)
    for index in range(_MAX_NUMBERED):
        candidate_name = safe_name if index == 0 else f"{stem}_{index}{extension}"
        candidate = os.path.join(folder, candidate_name)
        if not os.path.exists(candidate):
            return candidate
        if skip_existing and is_already_there(candidate, item.size):
            return candidate
    # Practically unreachable; fall back to a unique name rather than looping forever.
    return os.path.join(folder, f"{stem}_{uuid.uuid4().hex}{extension}")


def is_already_there(path: str, size: int) -> bool:
    """Same name + same size on disk. No hash check in this build."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) == size
    except OSError:
        return False
