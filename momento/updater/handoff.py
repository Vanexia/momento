"""Windows process coordination for a recording-safe Setup handoff."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, Any, Callable

from momento.util.single_instance import (
    UPDATE_GATE_MUTEX_NAME,
    SingleInstance,
    is_update_gate_active,
    is_valid_update_attempt_token,
)

_READY_EVENT_PREFIX = "Local\\Momento.GameRecorder.UpdateReady."
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000
_EVENT_MODIFY_STATE = 0x0002


class UpdateHandoffError(RuntimeError):
    """Raised when Setup cannot safely take ownership of an update."""


def readiness_event_name(attempt_token: str) -> str:
    """Return the per-attempt event name shared with the verified installer."""
    if not is_valid_update_attempt_token(attempt_token):
        raise ValueError("Update attempt token must be 64 lowercase hex characters")
    return f"{_READY_EVENT_PREFIX}{attempt_token}"


def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _close_windows_handle(handle: int | None) -> None:
    if not handle:
        return
    from ctypes import wintypes

    close_handle = _kernel32().CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _milliseconds(timeout: float | None) -> int:
    if timeout is None:
        return 0xFFFFFFFF
    if timeout <= 0:
        return 0
    return min(int(timeout * 1000 + 0.999), 0xFFFFFFFE)


class UpdateHandoffCoordination:
    """App-owned readiness event for one Setup process.

    Inno owns ``gate_name`` through ``SetupMutex``. Setup opens a SYNCHRONIZE
    handle to the live parent before signalling ``ready_event_name``. Momento
    keeps the event and its normal instance mutex alive until that signal and
    independently confirms that Setup's mutex exists.
    """

    def __init__(
        self,
        attempt_token: str,
        gate_name: str = UPDATE_GATE_MUTEX_NAME,
    ) -> None:
        if sys.platform != "win32":
            raise UpdateHandoffError("The updater handoff is available only on Windows")
        self.gate_name = gate_name
        self.ready_event_name = readiness_event_name(attempt_token)
        self._ready_handle: int | None = None
        self._create()

    def __enter__(self) -> "UpdateHandoffCoordination":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _create(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        create_event = kernel32.CreateEventW
        create_event.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        create_event.restype = wintypes.HANDLE

        ctypes.set_last_error(0)
        ready = create_event(None, True, False, self.ready_event_name)
        ready_error = ctypes.get_last_error()
        if not ready:
            self.close()
            raise UpdateHandoffError("Could not create the Setup readiness event") from ctypes.WinError(
                ready_error
            )
        self._ready_handle = int(ready)
        if ready_error == _ERROR_ALREADY_EXISTS:
            self.close()
            raise UpdateHandoffError("Setup readiness event already exists")

    def wait_until_ready(self, process: Any, timeout: float) -> bool:
        """Wait until Setup claims the gate and exact parent-process handle."""
        import ctypes
        from ctypes import wintypes

        if self._ready_handle is None:
            raise UpdateHandoffError("Update handoff coordination is closed")
        wait = _kernel32().WaitForSingleObject
        wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait.restype = wintypes.DWORD
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            result = wait(self._ready_handle, min(_milliseconds(remaining), 100))
            if result == _WAIT_OBJECT_0:
                if not is_update_gate_active(self.gate_name):
                    raise UpdateHandoffError("Setup signalled readiness without owning its mutex")
                return True
            if result == _WAIT_FAILED:
                raise UpdateHandoffError("Waiting for Setup readiness failed") from ctypes.WinError(
                    ctypes.get_last_error()
                )
            if result != _WAIT_TIMEOUT:
                raise UpdateHandoffError(f"Unexpected Setup readiness result: {result}")
            if process.poll() is not None or remaining <= 0:
                return False

    def close(self) -> None:
        ready = self._ready_handle
        self._ready_handle = None
        _close_windows_handle(ready)


class ParentProcessClaim:
    """Installer-side ownership of the exact parent process and update gate."""

    def __init__(self, parent_pid: int, parent_handle: int, gate_handle: int) -> None:
        self.parent_pid = parent_pid
        self._parent_handle: int | None = parent_handle
        self._gate_handle: int | None = gate_handle

    def __enter__(self) -> "ParentProcessClaim":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wait_for_parent_exit(self, timeout: float | None = None) -> bool:
        """Wait on the retained process object, never by polling its PID."""
        import ctypes
        from ctypes import wintypes

        if self._parent_handle is None:
            raise UpdateHandoffError("Parent-process claim is closed")
        wait = _kernel32().WaitForSingleObject
        wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait.restype = wintypes.DWORD
        result = wait(self._parent_handle, _milliseconds(timeout))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        if result == _WAIT_FAILED:
            raise UpdateHandoffError("Waiting for the parent process failed") from ctypes.WinError(
                ctypes.get_last_error()
            )
        raise UpdateHandoffError(f"Unexpected parent-process wait result: {result}")

    def close(self) -> None:
        parent = self._parent_handle
        gate = self._gate_handle
        self._parent_handle = None
        self._gate_handle = None
        _close_windows_handle(parent)
        _close_windows_handle(gate)


def claim_parent_process(
    parent_pid: int,
    attempt_token: str,
    *,
    gate_name: str = UPDATE_GATE_MUTEX_NAME,
) -> ParentProcessClaim:
    """Model Setup's exact-parent claim and signal app-side readiness.

    Inno itself retains ``SetupMutex``. This Python model opens a second gate
    handle so tests can prove uninterrupted ownership, opens the still-live
    parent process and proves that exact handle is unsignalled, then signals
    the per-attempt event. The subsequent wait is on that process handle rather
    than another lookup by PID.
    """
    if sys.platform != "win32":
        raise UpdateHandoffError("The updater handoff is available only on Windows")
    if not isinstance(parent_pid, int) or parent_pid <= 0:
        raise ValueError("Parent PID must be a positive integer")
    event_name = readiness_event_name(attempt_token)

    from ctypes import wintypes

    kernel32 = _kernel32()
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_mutex.restype = wintypes.HANDLE
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    open_event = kernel32.OpenEventW
    open_event.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_event.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    set_event = kernel32.SetEvent
    set_event.argtypes = (wintypes.HANDLE,)
    set_event.restype = wintypes.BOOL

    gate_handle: int | None = None
    parent_handle: int | None = None
    ready_handle: int | None = None
    try:
        parent = open_process(_SYNCHRONIZE, False, parent_pid)
        if not parent:
            raise UpdateHandoffError("Live parent process could not be opened")
        parent_handle = int(parent)
        if wait(parent_handle, 0) != _WAIT_TIMEOUT:
            raise UpdateHandoffError("Parent process exited before Setup claimed it")

        gate = open_mutex(_SYNCHRONIZE, False, gate_name)
        if not gate:
            raise UpdateHandoffError("Setup-owned update gate is unavailable")
        gate_handle = int(gate)

        ready = open_event(_EVENT_MODIFY_STATE, False, event_name)
        if not ready:
            raise UpdateHandoffError("App-owned readiness event is unavailable")
        ready_handle = int(ready)
        if not set_event(ready_handle):
            raise UpdateHandoffError("Could not signal Setup readiness")

        claim = ParentProcessClaim(parent_pid, parent_handle, gate_handle)
        parent_handle = None
        gate_handle = None
        return claim
    finally:
        _close_windows_handle(ready_handle)
        _close_windows_handle(parent_handle)
        _close_windows_handle(gate_handle)


def _same_open_file(path: Path, handle: BinaryIO) -> bool:
    try:
        return os.path.samestat(path.stat(), os.fstat(handle.fileno()))
    except (OSError, ValueError):
        return False


def _stop_failed_setup(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except Exception:
        # The still-running parent remains the install barrier even if Windows
        # refuses to terminate an installer process during failure cleanup.
        pass


def launch_update(
    installer: Path,
    *,
    attempt_token: str,
    single_instance: SingleInstance,
    verified_handle: BinaryIO,
    readiness_timeout: float = 15.0,
    _coordination_factory: Callable[[str, str], Any] = UpdateHandoffCoordination,
    _popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Any:
    """Launch verified Setup while the normal app mutex remains held.

    The caller must keep its update-quiescence lease and ``SingleInstance``
    until this function returns successfully. A success means Setup has
    signalled that it owns both the update gate and an exact process handle to
    this parent; it does not release the normal application lock.
    """
    try:
        readiness_event_name(attempt_token)
    except ValueError as exc:
        raise UpdateHandoffError("Invalid update attempt token") from exc
    if not single_instance.is_acquired:
        raise UpdateHandoffError("Normal Momento instance lock must remain held")

    try:
        installer_path = installer.resolve(strict=True)
    except OSError as exc:
        raise UpdateHandoffError("Verified installer path is unavailable") from exc
    if not installer_path.is_file() or installer_path.suffix.lower() != ".exe":
        raise UpdateHandoffError("Verified installer must be a regular executable")
    if not _same_open_file(installer_path, verified_handle):
        raise UpdateHandoffError("Verified handle does not identify the installer path")

    coordination = None
    process = None
    try:
        coordination = _coordination_factory(attempt_token, single_instance.update_gate_name)
        args = [
            str(installer_path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/MOMENTOUPDATE",
            f"/PARENTPID={os.getpid()}",
            f"/ATTEMPTTOKEN={attempt_token}",
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = _popen_factory(args, close_fds=True, creationflags=creationflags)

        # Keep a strong reference and prove the verified handle survived the
        # complete CreateProcess call before permitting the handoff to advance.
        os.fstat(verified_handle.fileno())
        if not single_instance.is_acquired:
            raise UpdateHandoffError("Normal Momento lock was released during Setup launch")
        if not coordination.wait_until_ready(process, readiness_timeout):
            raise UpdateHandoffError("Setup did not claim the update handoff in time")
    except Exception as exc:
        if process is not None:
            _stop_failed_setup(process)
        if isinstance(exc, UpdateHandoffError):
            raise
        raise UpdateHandoffError("Could not start the verified Momento installer") from exc
    finally:
        if coordination is not None:
            coordination.close()

    return process
