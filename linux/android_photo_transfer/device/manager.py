"""DeviceManager: tracks which phone is connected.

Port of upstream Core/Devices/DeviceManager.cs. Refreshes are driven
by the UI (and by the retry timer while LOCKED/ERROR), not by polling.
All public methods are blocking and safe to call from any thread; the
UI is expected to call refresh() from a worker thread.

The demo folder comes from the APT_DEMO_FOLDER environment variable
(unless a folder is passed in), exactly like upstream.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from . import folder_adapter, libmtp_backend
from .interface import DeviceAdapter
from .types import ConnectionState, PhoneListing, StorageRoot

_log = logging.getLogger("android_photo_transfer")

RETRY_INTERVAL_S = 3.0
CONNECT_TIMEOUT_S = 30.0


def _describe(exc: Exception) -> str:
    """Plain-language error text.

    core.errors is built in parallel with this module, so it is
    imported lazily and str(exc) is the fallback until it exists.
    """
    try:
        from ..core.errors import describe

        return describe(exc)
    except Exception:
        return str(exc) or exc.__class__.__name__


def _run_with_timeout(fn, timeout_s: float, label: str):
    """Run fn on a daemon thread; raise TimeoutError if it overruns.

    The thread is left running if it times out (it cannot be killed);
    it is a daemon, so it never blocks interpreter exit.
    """
    box: dict = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised on the caller thread
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True, name=f"apt-{label}")
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout_s:.0f} seconds")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class DeviceManager:
    """Tracks the connected phone and its storage roots."""

    def __init__(
        self,
        on_changed: Callable[[], None],
        demo_folder: str | None = None,
    ):
        self._on_changed = on_changed
        if demo_folder is None:
            demo_folder = os.environ.get("APT_DEMO_FOLDER")
        self._demo_folder = demo_folder if demo_folder else None
        self._lock = threading.Lock()
        self._disposed = False
        self._preferred_device_id: str | None = None
        self._current: DeviceAdapter | None = None
        self._phones: list[PhoneListing] = []
        self._storage_roots: list[StorageRoot] = []
        self._error_message: str | None = None
        self._state = ConnectionState.WAITING
        self._retry_timer: threading.Timer | None = None

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def phones(self) -> list[PhoneListing]:
        return list(self._phones)

    @property
    def current(self) -> DeviceAdapter | None:
        return self._current

    @property
    def storage_roots(self) -> list[StorageRoot]:
        return list(self._storage_roots)

    @property
    def error_message(self) -> str | None:
        return self._error_message

    # -- public API --------------------------------------------------------

    def refresh(self) -> None:
        """Re-list phones and (re)connect if needed. Blocking."""
        if self._disposed:
            return
        with self._lock:
            if self._disposed:
                return
            try:
                self._refresh_locked()
            except Exception as exc:
                _log.error("Could not connect to the phone: %s", exc, exc_info=True)
                self._drop_current()
                self._error_message = _describe(exc)
                self._state = ConnectionState.ERROR
                self._schedule_retry()
        # Like upstream, notify after releasing the gate.
        self._raise_changed()

    def reconnect(self) -> None:
        """Drop the current connection and refresh (user refresh)."""
        if self._disposed:
            return
        with self._lock:
            if self._current is not None:
                _log.info(
                    "Reconnecting to %s (user refresh)",
                    self._current.info.name,
                )
            self._drop_current()
        self.refresh()

    def select_phone(self, device_id: str) -> None:
        """Prefer this phone and connect to it (no-op if already current)."""
        self._preferred_device_id = device_id
        if self._current is not None and self._current.info.device_id == device_id:
            return
        if self._disposed:
            return
        with self._lock:
            self._drop_current()
        self.refresh()

    def shutdown(self) -> None:
        """Stop the retry timer and close the current connection."""
        self._disposed = True
        self._stop_retry()
        acquired = self._lock.acquire(timeout=2.0)
        try:
            self._drop_current()
        finally:
            if acquired:
                self._lock.release()

    # -- internals (called with the lock held unless noted) ----------------

    def _refresh_locked(self) -> None:
        phones = list(libmtp_backend.list_phones())
        if self._demo_folder and os.path.isdir(self._demo_folder):
            phones.extend(folder_adapter.list_phones(self._demo_folder))
        self._phones = phones

        current = self._current
        if current is not None and all(
            p.device_id != current.info.device_id for p in phones
        ):
            _log.info("Device disconnected: %s", current.info.name)
            self._drop_current()
            current = None
        elif current is not None:
            try:
                connected = current.is_connected
            except Exception:
                connected = False
            if not connected:
                # Still listed, but this connection died (USB glitch,
                # phone reset the session): replace it with a fresh one
                # instead of failing every request.
                _log.info(
                    "Connection to %s is no longer usable; reconnecting",
                    current.info.name,
                )
                self._drop_current()
                current = None

        if current is not None:
            target = next(
                p for p in phones if p.device_id == current.info.device_id
            )
        else:
            target = next(
                (p for p in phones if p.device_id == self._preferred_device_id),
                None,
            )
            if target is None and phones:
                target = phones[0]

        if target is None:
            self._stop_retry()
            self._state = ConnectionState.WAITING
            return

        if current is None or self._state in (
            ConnectionState.LOCKED,
            ConnectionState.ERROR,
        ):
            self._connect(target)

    def _connect(self, target: PhoneListing) -> None:
        was_locked = self._state == ConnectionState.LOCKED
        self._drop_current()
        if not was_locked:
            self._state = ConnectionState.CONNECTING
            self._raise_changed()
            _log.info("Connecting to %s", target.name)

        if target.device_id.startswith(folder_adapter.DEVICE_ID_PREFIX):
            device = folder_adapter.connect(target.device_id)
        else:
            device = _run_with_timeout(
                lambda: libmtp_backend.connect(target.device_id),
                CONNECT_TIMEOUT_S,
                f"connect to {target.name}",
            )
        self._current = device

        roots = _run_with_timeout(
            device.get_storage_roots, CONNECT_TIMEOUT_S, "list storage"
        )
        accessible = False
        for root in roots:
            try:
                if _run_with_timeout(
                    lambda r=root: device.folder_has_entries(r.path),
                    CONNECT_TIMEOUT_S,
                    "probe storage",
                ):
                    accessible = True
                    break
            except Exception as exc:
                _log.warning("Storage %r is not readable yet: %s", root.name, exc)

        self._storage_roots = list(roots)
        self._error_message = None

        if accessible:
            self._stop_retry()
            _log.info(
                "Device ready: %s (%d storage location(s))",
                device.info.name,
                len(roots),
            )
            self._state = ConnectionState.READY
        else:
            if not was_locked:
                _log.info(
                    "%s is connected but its storage is unavailable "
                    "(locked or not in File Transfer mode)",
                    device.info.name,
                )
            self._state = ConnectionState.LOCKED
            self._schedule_retry()

    def _drop_current(self) -> None:
        current, self._current = self._current, None
        self._storage_roots = []
        if current is not None:
            try:
                current.close()
            except Exception:
                _log.debug("Error closing device adapter", exc_info=True)

    def _schedule_retry(self) -> None:
        if self._disposed:
            return
        self._stop_retry()
        timer = threading.Timer(RETRY_INTERVAL_S, self._on_retry)
        timer.daemon = True
        timer.start()
        self._retry_timer = timer

    def _stop_retry(self) -> None:
        timer, self._retry_timer = self._retry_timer, None
        if timer is not None:
            timer.cancel()

    def _on_retry(self) -> None:
        if self._disposed:
            return
        self.refresh()

    def _raise_changed(self) -> None:
        if self._disposed:
            return
        try:
            self._on_changed()
        except Exception:
            _log.exception("on_changed callback failed")


__all__ = [
    "CONNECT_TIMEOUT_S",
    "RETRY_INTERVAL_S",
    "DeviceManager",
]
