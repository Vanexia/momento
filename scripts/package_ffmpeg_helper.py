"""Create the deterministic downloadable archive for Momento's FFmpeg helper."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import zipfile
from pathlib import Path


FILES = ("ffmpeg.exe", "ffprobe.exe", "LICENSE.txt", "README.txt", "SHA256SUMS.txt")
PAYLOAD_FILES = FILES[:-1]
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    missing = [name for name in FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"helper package inputs are missing: {', '.join(missing)}")
    declared: dict[str, str] = {}
    for line in (source / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        digest, separator, filename = line.partition(" ")
        filename = filename.lstrip(" *")
        if not separator or len(digest) != 64 or not filename:
            raise RuntimeError("malformed helper SHA256SUMS.txt")
        declared[filename] = digest.casefold()
    if set(declared) != set(PAYLOAD_FILES):
        raise RuntimeError("helper SHA256SUMS.txt does not cover the exact payload")
    for name in PAYLOAD_FILES:
        if declared[name] != _sha256(source / name):
            raise RuntimeError(f"helper payload hash mismatch: {name}")

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    if partial.exists():
        partial.unlink()
    with zipfile.ZipFile(partial, "w", compresslevel=9) as archive:
        for name in FILES:
            with archive.open(_info(name), "w") as target:
                with (source / name).open("rb") as handle:
                    shutil.copyfileobj(handle, target, length=1024 * 1024)
    os.replace(partial, output)

    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"helper archive CRC failure: {bad}")
        if tuple(archive.namelist()) != FILES:
            raise RuntimeError("helper archive layout changed")
    print(f"PASS - deterministic helper archive: {output}")
    print(f"SHA256 - {_sha256(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
