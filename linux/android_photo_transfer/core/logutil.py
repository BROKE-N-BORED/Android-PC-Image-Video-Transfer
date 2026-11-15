"""Logging setup for the Android Photo Transfer Linux port.

Ports upstream Infrastructure/Logger.cs: a rolling file log (app.log,
app.log.1, app.log.2; 1 MB each) under the XDG logs dir. Logging must
never crash or slow the app, so setup failures fall back to a plain
stderr handler and are swallowed.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "android_photo_transfer"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 2  # app.log + app.log.1 + app.log.2, like upstream


def default_logs_dir() -> str:
    """~/.local/share/android-photo-transfer/logs (honours XDG_DATA_HOME)."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(data_home, "android-photo-transfer", "logs")


def setup_logging(logs_dir: str | None = None) -> logging.Logger:
    """Configure the "android_photo_transfer" logger; returns it.

    Idempotent: calling it again returns the same logger without adding
    duplicate handlers. Never raises.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler: logging.Handler
    try:
        folder = logs_dir or default_logs_dir()
        os.makedirs(folder, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(folder, "app.log"),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except Exception:
        # A full or read-only disk must not take the app down.
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def log_exception(
    logger: logging.Logger, message: str, exc: Exception | None = None
) -> None:
    """Log a warning with the exception type and message attached."""
    if exc is None:
        logger.warning(message)
    else:
        logger.warning("%s | %s: %s", message, type(exc).__name__, exc)
