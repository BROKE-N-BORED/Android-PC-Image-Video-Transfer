"""Core engine of the Android Photo Transfer Linux port (stdlib only, no Qt).

classifier: file/folder categories. scanner: MediaItem, scan(), MediaQuery.
destination: never-overwrite paths. transfer: TransferManager engine.
history: SQLite transfer history. settings: JSON user settings.
logutil: rolling file logging. errors: plain-language error messages.
"""
from .classifier import (
    APP_EXTS,
    ARCHIVE_EXTS,
    AUDIO_EXTS,
    DECODABLE_EXTS,
    DOCUMENT_EXTS,
    PHOTO_EXTS,
    VIDEO_EXTS,
    can_decode,
    file_category,
    folder_category,
    is_photo,
    is_video,
)
from .destination import (
    destination_folder_for,
    is_already_there,
    resolve,
    sanitize_file_name,
    sanitize_relative_folder,
)
from .errors import (
    PHONESTOPPED,
    PHONE_STOPPED_RESUME,
    InsufficientSpaceError,
    TransferVerificationError,
    describe,
    format_bytes,
    is_destination_fatal,
    is_disk_full,
)
from .history import HistoryDatabase
from .logutil import LOGGER_NAME, default_logs_dir, log_exception, setup_logging
from .scanner import MediaItem, MediaQuery, scan
from .settings import (
    CACHE_MAX_MB,
    CACHE_MIN_MB,
    Settings,
    SettingsManager,
    config_dir,
    data_dir,
    default_destination,
    default_history_path,
    default_settings_path,
)
from .transfer import (
    BUFFER_SIZE,
    CONSECUTIVE_FAILURE_LIMIT,
    FREE_SPACE_MARGIN,
    TransferItem,
    TransferManager,
    TransferProgress,
    TransferResult,
)

__all__ = [
    "APP_EXTS", "ARCHIVE_EXTS", "AUDIO_EXTS", "DECODABLE_EXTS",
    "DOCUMENT_EXTS", "PHOTO_EXTS", "VIDEO_EXTS",
    "can_decode", "file_category", "folder_category", "is_photo", "is_video",
    "destination_folder_for", "is_already_there", "resolve",
    "sanitize_file_name", "sanitize_relative_folder",
    "PHONESTOPPED", "PHONE_STOPPED_RESUME", "InsufficientSpaceError", "TransferVerificationError",
    "describe", "format_bytes", "is_destination_fatal", "is_disk_full",
    "HistoryDatabase",
    "LOGGER_NAME", "default_logs_dir", "log_exception", "setup_logging",
    "MediaItem", "MediaQuery", "scan",
    "CACHE_MAX_MB", "CACHE_MIN_MB", "Settings", "SettingsManager",
    "config_dir", "data_dir", "default_destination", "default_history_path",
    "default_settings_path",
    "BUFFER_SIZE", "CONSECUTIVE_FAILURE_LIMIT", "FREE_SPACE_MARGIN",
    "TransferItem", "TransferManager", "TransferProgress", "TransferResult",
]
