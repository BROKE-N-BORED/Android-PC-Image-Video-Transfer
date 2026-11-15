"""File classification for the Android Photo Transfer Linux port.

Ported exactly from upstream Core/Media/MediaClassifier.cs. Extension
lists are verbatim from that file:

- PHOTO_EXTS: 20 entries (jpg jpeg jpe png heic heif webp gif bmp tif
  tiff avif dng raw arw cr2 cr3 nef orf rw2)
- VIDEO_EXTS: 12 entries (mp4 mov m4v 3gp 3g2 mkv avi webm wmv mts m2ts ts)

Note: the port brief said 13 video extensions, but MediaClassifier.cs
has 12; the source file wins.
"""
from __future__ import annotations

import os

PHOTO_EXTS = frozenset(
    {
        ".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".webp", ".gif",
        ".bmp", ".tif", ".tiff", ".avif", ".dng", ".raw", ".arw", ".cr2",
        ".cr3", ".nef", ".orf", ".rw2",
    }
)

VIDEO_EXTS = frozenset(
    {
        ".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mkv", ".avi", ".webm",
        ".wmv", ".mts", ".m2ts", ".ts",
    }
)

# Formats a thumbnail decoder can handle out of the box (HEIC/WebP/AVIF
# work when the system codec is installed). Ported from upstream.
DECODABLE_EXTS = frozenset(
    {
        ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff",
        ".heic", ".heif", ".webp", ".avif",
    }
)

DOCUMENT_EXTS = frozenset(
    {
        ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xls",
        ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp", ".epub", ".html",
        ".htm", ".json", ".xml", ".vcf", ".ics",
    }
)

AUDIO_EXTS = frozenset(
    {
        ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac",
        ".amr", ".wma", ".mid", ".midi", ".3ga",
    }
)

ARCHIVE_EXTS = frozenset(
    {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
)

APP_EXTS = frozenset({".apk", ".apks", ".xapk", ".apkm"})


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def is_photo(name: str) -> bool:
    return _extension(name) in PHOTO_EXTS


def is_video(name: str) -> bool:
    return _extension(name) in VIDEO_EXTS


def can_decode(name: str) -> bool:
    """True when the file is in a format thumbnails can decode directly."""
    return _extension(name) in DECODABLE_EXTS


def file_category(name: str) -> str:
    """Files-tab category: Photos | Videos | Documents | Audio | Archives | Apps | Other."""
    if is_photo(name):
        return "Photos"
    if is_video(name):
        return "Videos"
    ext = _extension(name)
    if ext in DOCUMENT_EXTS:
        return "Documents"
    if ext in AUDIO_EXTS:
        return "Audio"
    if ext in ARCHIVE_EXTS:
        return "Archives"
    if ext in APP_EXTS:
        return "Apps"
    return "Other"


def folder_category(phone_path: str) -> str:
    """Category from the folder relative to the storage root, e.g. "DCIM/Camera".

    Ported from MediaClassifier.GetCategory: "screenshot" anywhere in the
    path wins first, then DCIM, Download/Downloads, Pictures.
    """
    folder = phone_path.replace("\\", "/").strip("/").lower()
    if "screenshot" in folder:
        return "Screenshots"
    if folder == "dcim" or folder.startswith("dcim/"):
        return "Camera"
    if (
        folder == "download"
        or folder.startswith("download/")
        or folder == "downloads"
        or folder.startswith("downloads/")
    ):
        return "Downloads"
    if folder == "pictures" or folder.startswith("pictures/"):
        return "Pictures"
    return "Other"
