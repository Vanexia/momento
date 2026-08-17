"""Require the release builder to use the exact pinned environment."""

from __future__ import annotations

import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")


def main() -> int:
    failures: list[str] = []
    if sys.version_info[:2] != (3, 12):
        failures.append(f"Python is {sys.version_info.major}.{sys.version_info.minor}, expected 3.12")
    for line in (ROOT / "constraints-release.txt").read_text(encoding="utf-8").splitlines():
        match = PIN.match(line.strip())
        if not match:
            continue
        name, expected = match.groups()
        try:
            actual = version(name)
        except PackageNotFoundError:
            failures.append(f"{name} is missing (expected {expected})")
            continue
        if actual != expected:
            failures.append(f"{name} is {actual}, expected {expected}")
    if failures:
        for failure in failures:
            print(f"FAIL - {failure}")
        return 1
    print("PASS - Python and all constrained distributions match the release lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
