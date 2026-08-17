"""Single-instance and installer-handoff enforcement for Momento.

The lock is held by the process for its lifetime — if the process crashes, the
OS releases the lock automatically. The lock file itself is best-effort
deleted on clean exit. Windows launches also respect Inno Setup's update gate.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

from momento.util.paths import appdata_dir

logger = logging.getLogger(__name__)

INSTALLER_MUTEX_NAME = "Momento.GameRecorder.Instance"
UPDATE_GATE_MUTEX_NAME = "Momento.GameRecorder.Update"
_UPDATE_ATTEMPT_TOKEN = re.compile(r"[0-9a-f]{64}")


def is_valid_update_attempt_token(value: str | None) -> bool:
    return isinstance(value, str) and _UPDATE_ATTEMPT_TOKEN.fullmatch(value) is not None


def _windows_named_mutex_exists(name: str) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_mutex.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    handle = open_mutex(0x00100000, False, name)  # SYNCHRONIZE
    if handle:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
        return True

    error = ctypes.get_last_error()
    if error == 2:  # ERROR_FILE_NOT_FOUND
        return False
    raise ctypes.WinError(error)


def is_update_gate_active(name: str = UPDATE_GATE_MUTEX_NAME) -> bool:
    """Return whether Setup currently owns the update handoff gate.

    Windows coordination failures are treated as an active gate. Starting a
    second application process is less safe than asking the user to retry.
    """
    if sys.platform != "win32":
        return False
    try:
        return _windows_named_mutex_exists(name)
    except OSError:
        logger.exception("Could not inspect the Momento update gate")
        return True


def wait_for_update_gate_release(
    attempt_token: str,
    *,
    timeout: float = 15.0,
    name: str = UPDATE_GATE_MUTEX_NAME,
    poll_interval: float = 0.05,
) -> bool:
    """Bound a confirmed post-update launch's wait for Inno's SetupMutex.

    Token authenticity is established by the updater attempt store before this
    API is called. This boundary also requires the installer's exact token
    shape so an ordinary or malformed launch never receives wait privileges.
    """
    if not is_valid_update_attempt_token(attempt_token):
        raise ValueError("Updated launch token must be 64 lowercase hex characters")
    if timeout < 0:
        raise ValueError("Update gate wait timeout cannot be negative")
    if poll_interval <= 0:
        raise ValueError("Update gate poll interval must be positive")
    if sys.platform != "win32":
        return True

    deadline = time.monotonic() + timeout
    while is_update_gate_active(name):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval, remaining))
    return True


class AlreadyRunningError(RuntimeError):
    """Raised when another Momento instance is already holding the lock."""


class SingleInstance:
    """Context manager that acquires an exclusive lock on a file in APPDATA.

    Usage::

        try:
            with SingleInstance():
                run_app()
        except AlreadyRunningError:
            print("Momento is already running.")
            sys.exit(1)
    """

    def __init__(
        self,
        lock_path: Path | None = None,
        *,
        mutex_name: str = INSTALLER_MUTEX_NAME,
        update_gate_name: str = UPDATE_GATE_MUTEX_NAME,
    ) -> None:
        self._lock_path = lock_path or (appdata_dir() / "momento.lock")
        self._mutex_name = mutex_name
        self._update_gate_name = update_gate_name
        self._fh = None  # type: ignore[assignment]
        self._mutex_handle = None

    @property
    def is_acquired(self) -> bool:
        if self._fh is None:
            return False
        return sys.platform != "win32" or self._mutex_handle is not None

    @property
    def update_gate_name(self) -> str:
        return self._update_gate_name

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def acquire(
        self,
        *,
        updated_token: str | None = None,
        update_wait_timeout: float = 15.0,
    ) -> None:
        if self.is_acquired:
            raise RuntimeError("Single-instance lock is already acquired")
        wait_deadline = time.monotonic() + max(update_wait_timeout, 0)
        if sys.platform == "win32" and is_update_gate_active(self._update_gate_name):
            if not is_valid_update_attempt_token(updated_token):
                raise AlreadyRunningError("Momento is being updated")
            if not wait_for_update_gate_release(
                updated_token,
                timeout=max(0.0, wait_deadline - time.monotonic()),
                name=self._update_gate_name,
            ):
                raise AlreadyRunningError("Momento update is still finishing")

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "a+b")

        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            create_mutex.restype = wintypes.HANDLE
            self._mutex_handle = create_mutex(None, False, self._mutex_name)
            if not self._mutex_handle:
                error = ctypes.get_last_error()
                fh.close()
                raise OSError(error, "Could not create the installer coordination mutex")

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                fh.close()
                self._close_mutex()
                raise AlreadyRunningError(
                    f"Another Momento instance is already running (lock: {self._lock_path})"
                ) from e

            gate_active = is_update_gate_active(self._update_gate_name)
            if gate_active and is_valid_update_attempt_token(updated_token):
                gate_active = not wait_for_update_gate_release(
                    updated_token,
                    timeout=max(0.0, wait_deadline - time.monotonic()),
                    name=self._update_gate_name,
                )
            if gate_active:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                fh.close()
                self._close_mutex()
                raise AlreadyRunningError("Momento is being updated")
        else:
            # Best-effort on non-Windows for dev convenience
            import fcntl  # type: ignore[import-not-found]

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                fh.close()
                raise AlreadyRunningError(
                    f"Another Momento instance is already running (lock: {self._lock_path})"
                ) from e

        # Record PID for visibility (not used programmatically — the lock is the
        # source of truth).
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
        self._fh = fh
        logger.info("Acquired single-instance lock (pid=%d)", os.getpid())

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            self._close_mutex()
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            fh.close()
        except Exception:
            logger.exception("Error closing single-instance lock handle")
        try:
            self._lock_path.unlink()
        except OSError:
            pass
        self._close_mutex()
        logger.info("Released single-instance lock")

    def _close_mutex(self) -> None:
        handle = self._mutex_handle
        self._mutex_handle = None
        if handle is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            if not close_handle(handle):
                logger.warning("Could not close the installer coordination mutex")
        except Exception:
            logger.exception("Error closing installer mutex")
