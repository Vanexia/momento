"""Smoke test for SingleInstance lock."""

from __future__ import annotations

import sys
import tempfile
import ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.util.single_instance import (  # noqa: E402
    INSTALLER_MUTEX_NAME,
    AlreadyRunningError,
    SingleInstance,
)


def mutex_exists() -> bool:
    handle = ctypes.windll.kernel32.OpenMutexW(
        0x00100000, False, INSTALLER_MUTEX_NAME
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def main() -> int:
    lock = Path(tempfile.gettempdir()) / "momento_single_test.lock"
    lock.unlink(missing_ok=True)

    a = SingleInstance(lock)
    a.acquire()
    print("A acquired")
    if not mutex_exists():
        print("FAIL: installer mutex is missing")
        return 3

    b = SingleInstance(lock)
    try:
        b.acquire()
        print("FAIL: B unexpectedly acquired")
        return 2
    except AlreadyRunningError as e:
        print(f"OK B blocked: {e}")

    a.release()
    print("A released")
    if mutex_exists():
        print("FAIL: installer mutex remained after release")
        return 4

    c = SingleInstance(lock)
    c.acquire()
    print("OK C re-acquired after release")
    c.release()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
