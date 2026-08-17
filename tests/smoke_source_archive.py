"""Scan the exact corresponding-source ZIP before it enters the installer."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path, PurePosixPath

from privacy_scan import (
    binary_contains_blocked_identity,
    contains_private_windows_user_path,
)


RUNTIME_NAMES = {
    "config.json", "momento.log", "momento.lock", "window_state.ini",
    "youtube_token.dat", "youtube_avatar.png", "client_secrets.json",
}
RUNTIME_SUFFIXES = (".thumb.jpg", ".bookmarks.json", ".momento.json")
PRIVATE_ROOTS = {".agents", ".claude"}
REQUIRED_RELEASE_FILES = {
    "Momento-0.2.4-source.zip": {
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
            if name in RUNTIME_NAMES or name.endswith(RUNTIME_SUFFIXES):
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
