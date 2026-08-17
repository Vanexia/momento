"""Smoke tests for the process lock and updater gate."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.util.single_instance import (  # noqa: E402
    INSTALLER_MUTEX_NAME,
    UPDATE_GATE_MUTEX_NAME,
    AlreadyRunningError,
    SingleInstance,
    is_update_gate_active,
    wait_for_update_gate_release,
)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _create_windows_mutex(name: str) -> int:
    import ctypes
    from ctypes import wintypes

    create_mutex = ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW
    create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    handle = create_mutex(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _mutex_exists(name: str) -> bool:
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    open_mutex = ctypes.WinDLL("kernel32", use_last_error=True).OpenMutexW
    open_mutex.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_mutex.restype = wintypes.HANDLE
    handle = open_mutex(0x00100000, False, name)
    if not handle:
        return False
    _close_windows_handle(int(handle))
    return True


def _check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS: {label}")


def test_process_lock() -> None:
    lock = Path(tempfile.gettempdir()) / f"momento_single_test_{os.getpid()}.lock"
    lock.unlink(missing_ok=True)
    mutex_name = f"{INSTALLER_MUTEX_NAME}.Test.{os.getpid()}"
    gate_name = f"{UPDATE_GATE_MUTEX_NAME}.Test.Process.{os.getpid()}"

    first = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
    second = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
    first.acquire()
    try:
        _check("first instance reports its acquired state", first.is_acquired)
        if sys.platform == "win32":
            _check("installer coordination mutex exists", _mutex_exists(mutex_name))
        try:
            second.acquire()
        except AlreadyRunningError:
            pass
        else:
            raise AssertionError("second instance acquired the process lock")
    finally:
        first.release()

    _check("released instance reports its state", not first.is_acquired)
    if sys.platform == "win32":
        _check("installer coordination mutex closes on release", not _mutex_exists(mutex_name))

    third = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
    third.acquire()
    third.release()
    _check("process lock can be reacquired after release", True)


def test_update_gate_blocks_new_instance() -> None:
    if sys.platform != "win32":
        _check("Windows update-gate check is skipped off Windows", True)
        return

    suffix = f"{os.getpid()}"
    lock = Path(tempfile.gettempdir()) / f"momento_gate_test_{suffix}.lock"
    lock.unlink(missing_ok=True)
    mutex_name = f"{INSTALLER_MUTEX_NAME}.Test.Gate.{suffix}"
    gate_name = f"{UPDATE_GATE_MUTEX_NAME}.Test.{suffix}"
    gate_handle = _create_windows_mutex(gate_name)
    try:
        _check("active update gate is discoverable", is_update_gate_active(gate_name))
        blocked = SingleInstance(
            lock,
            mutex_name=mutex_name,
            update_gate_name=gate_name,
        )
        try:
            blocked.acquire()
        except AlreadyRunningError:
            pass
        else:
            blocked.release()
            raise AssertionError("new Momento instance entered through an active update gate")
        _check("blocked launch does not leave the application mutex behind", not _mutex_exists(mutex_name))
    finally:
        _close_windows_handle(gate_handle)

    _check("closed update gate is no longer active", not is_update_gate_active(gate_name))
    resumed = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
    resumed.acquire()
    resumed.release()
    _check("normal launch resumes after the update gate closes", True)


def test_confirmed_update_relaunch_waits_for_gate() -> None:
    if sys.platform != "win32":
        _check("confirmed-update gate wait is skipped off Windows", True)
        return

    suffix = f"{os.getpid()}"
    gate_name = f"{UPDATE_GATE_MUTEX_NAME}.Test.Relaunch.{suffix}"
    mutex_name = f"{INSTALLER_MUTEX_NAME}.Test.Relaunch.{suffix}"
    lock = Path(tempfile.gettempdir()) / f"momento_relaunch_test_{suffix}.lock"
    lock.unlink(missing_ok=True)
    gate_handle = _create_windows_mutex(gate_name)

    def finish_setup() -> None:
        time.sleep(0.1)
        _close_windows_handle(gate_handle)

    closer = threading.Thread(target=finish_setup, daemon=True)
    closer.start()
    started = time.monotonic()
    instance = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
    instance.acquire(updated_token="a" * 64, update_wait_timeout=1.0)
    elapsed = time.monotonic() - started
    try:
        _check("confirmed update relaunch waits for SetupMutex release", elapsed >= 0.05)
        _check("confirmed update relaunch acquires after SetupMutex release", instance.is_acquired)
    finally:
        instance.release()
        closer.join(timeout=2)

    held_gate = _create_windows_mutex(gate_name)
    try:
        started = time.monotonic()
        released = wait_for_update_gate_release(
            "b" * 64,
            timeout=0.1,
            name=gate_name,
            poll_interval=0.01,
        )
        elapsed = time.monotonic() - started
        _check("confirmed update gate wait has a timeout", not released and elapsed < 0.5)

        ordinary = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
        try:
            ordinary.acquire()
        except AlreadyRunningError:
            pass
        else:
            ordinary.release()
            raise AssertionError("ordinary launch waited through an active update")
        _check("ordinary launch remains blocked without an update token", True)

        invalid = SingleInstance(lock, mutex_name=mutex_name, update_gate_name=gate_name)
        try:
            invalid.acquire(updated_token="A" * 64, update_wait_timeout=0.2)
        except AlreadyRunningError:
            pass
        else:
            invalid.release()
            raise AssertionError("invalid update token received relaunch privileges")
        _check("non-lowercase update token cannot wait through the gate", True)
    finally:
        _close_windows_handle(held_gate)


def main() -> int:
    test_process_lock()
    test_update_gate_blocks_new_instance()
    test_confirmed_update_relaunch_waits_for_gate()
    print("PASS: single-instance smoke checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
