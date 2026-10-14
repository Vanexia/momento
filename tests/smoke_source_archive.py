"""Scan the exact corresponding-source ZIP before it enters the installer."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path, PurePosixPath

from privacy_scan import (
    binary_contains_blocked_identity,
    contains_private_windows_user_path,
    is_private_runtime_path,
)


PRIVATE_ROOTS = {".agents", ".claude"}
PRIVATE_NAMES = {"agents.md", "claude.md", "skills-lock.json"}
PRIVATE_PREFIXES = {("docs", "superpowers")}
REQUIRED_RELEASE_FILES = {
    "Mementos-0.2.11-source.zip": {
        "mementos/__init__.py",
        "mementos/__main__.py",
        "momento/ui/folder_dialog.py",
        "momento/core/mic_monitor.py",
        "momento/ui/level_meter.py",
        "tests/smoke_mic_calibration.py",
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
    "Mementos-0.2.10-source.zip": {
        "mementos/__init__.py",
        "mementos/__main__.py",
        "momento/ui/folder_dialog.py",
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
    "Mementos-0.2.9-source.zip": {
        "mementos/__init__.py",
        "mementos/__main__.py",
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
    "Momento-0.2.8-source.zip": {
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
    "Momento-0.2.5-source.zip": {
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
    "Momento-0.2.6-source.zip": {
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
    "Momento-0.2.7-source.zip": {
        "build/pyav_runtime.json",
        "scripts/build_pyav_runtime.ps1",
        "scripts/build_pyav_runtime.sh",
        "scripts/verify_pyav_runtime.py",
    },
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: smoke_source_archive.py <source.zip>")
        return 2
    archive = Path(sys.argv[1])
    failures: list[str] = []
    with zipfile.ZipFile(archive) as source:
        members = {info.filename for info in source.infolist() if not info.is_dir()}
        for required in REQUIRED_RELEASE_FILES.get(archive.name, set()):
            if required not in members:
                failures.append(f"missing release source: {required}")
        for info in source.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if parts and parts[0].casefold() in PRIVATE_ROOTS:
                failures.append(f"local agent file: {info.filename}")
            name = PurePosixPath(info.filename).name.casefold()
            folded_parts = tuple(part.casefold() for part in parts)
            if name in PRIVATE_NAMES or any(
                folded_parts[: len(prefix)] == prefix for prefix in PRIVATE_PREFIXES
            ):
                failures.append(f"internal agent file: {info.filename}")
            if is_private_runtime_path(info.filename):
                failures.append(f"private runtime file: {info.filename}")
            data = source.read(info)
            if binary_contains_blocked_identity(data):
                failures.append(f"private identity string: {info.filename}")
            text = data.decode("utf-8", errors="ignore")
            if contains_private_windows_user_path(text):
                failures.append(f"private Windows user path: {info.filename}")
    if failures:
        for failure in failures:
            print(f"FAIL - {failure}")
        return 1
    print("PASS - corresponding source archive is privacy-clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
