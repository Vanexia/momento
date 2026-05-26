"""Smoke test for SingleInstance lock."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.util.single_instance import AlreadyRunningError, SingleInstance  # noqa: E402


def main() -> int:
    lock = Path(tempfile.gettempdir()) / "momento_single_test.lock"
    lock.unlink(missing_ok=True)

    a = SingleInstance(lock)
    a.acquire()
    print("A acquired")

    b = SingleInstance(lock)
    try:
        b.acquire()
        print("FAIL: B unexpectedly acquired")
        return 2
    except AlreadyRunningError as e:
        print(f"OK B blocked: {e}")

    a.release()
    print("A released")

    c = SingleInstance(lock)
    c.acquire()
    print("OK C re-acquired after release")
    c.release()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
