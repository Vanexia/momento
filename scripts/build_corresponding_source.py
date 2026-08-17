"""Download, verify, and package Momento's native corresponding sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "build" / "corresponding_sources.json"
CHUNK_SIZE = 1024 * 1024
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_manifest(data: dict) -> list[dict[str, str]]:
    if data.get("schema_version") != 1:
        raise ValueError("unsupported corresponding-source manifest schema")
    if not data.get("release") or not data.get("bundle_name"):
        raise ValueError("manifest must identify its release and bundle name")

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("manifest must contain at least one source")

    filenames: set[str] = set()
    for source in sources:
        required = {"name", "version", "role", "filename", "url", "sha256"}
        if not isinstance(source, dict) or not required <= source.keys():
            raise ValueError("every source entry must contain all required fields")
        filename = source["filename"]
        if Path(filename).name != filename or filename in filenames:
            raise ValueError(f"unsafe or duplicate source filename: {filename}")
        filenames.add(filename)
        expected = source["sha256"]
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError(f"invalid SHA-256 for {filename}")
        if not source["url"].startswith("https://"):
            raise ValueError(f"source URL is not HTTPS: {filename}")
    return sources


def _download(source: dict[str, str], cache: Path, *, offline: bool) -> Path:
    destination = cache / source["filename"]
    expected = source["sha256"]
    if destination.is_file():
        actual = sha256(destination)
        if actual == expected:
            print(f"PASS - cached {source['filename']}")
            return destination
        raise RuntimeError(
            f"cached source hash mismatch for {source['filename']}: {actual}"
        )
    if offline:
        raise FileNotFoundError(f"offline source is missing: {destination}")

    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()
    request = urllib.request.Request(
        source["url"],
        headers={"User-Agent": "Momento-source-bundler/1.0"},
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"FETCH - {source['name']} {source['version']} (attempt {attempt}/3)")
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    with partial.open("wb") as handle:
                        shutil.copyfileobj(response, handle, length=CHUNK_SIZE)
            except urllib.error.URLError:
                curl = shutil.which("curl.exe") or shutil.which("curl")
                if not curl:
                    raise
                subprocess.run(
                    [
                        curl,
                        "--fail",
                        "--location",
                        "--proto",
                        "=https",
                        "--tlsv1.2",
                        "--silent",
                        "--show-error",
                        "--output",
                        str(partial),
                        source["url"],
                    ],
                    check=True,
                    timeout=300,
                )
            actual = sha256(partial)
            if actual != expected:
                raise RuntimeError(
                    f"source hash mismatch for {source['filename']}: {actual}"
                )
            os.replace(partial, destination)
            print(f"PASS - verified {source['filename']}")
            return destination
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            last_error = error
            if partial.exists():
                partial.unlink()
            if attempt < 3:
                time.sleep(attempt * 2)
    raise RuntimeError(f"could not fetch {source['filename']}: {last_error}")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _readme(data: dict, sources: list[dict[str, str]]) -> bytes:
    pyav = data["binary_provenance"]["pyav_wheel"]
    lines = [
        f"Momento {data['release']} third-party corresponding source",
        "",
        "This archive accompanies the Momento Windows binary release.",
        "It contains the exact source archives and build recipe used by the",
        "bundled FFmpeg helper, PyAV native runtime, and PyQt/Qt runtime.",
        "",
        "Build records:",
        f"- Custom PyAV wheel: {pyav['filename']}",
        f"- Custom PyAV wheel SHA-256: {pyav['sha256']}",
        f"- PyAV runtime contract: {pyav['runtime_manifest']}",
        f"- PyAV build recipes: {pyav['build_recipe']} and {pyav['native_build_recipe']}",
        f"- PyAV SOURCE_DATE_EPOCH: {pyav['source_date_epoch']}",
        "- PyQt/Qt wheel identities: binary_provenance in manifest.json",
        "- Qt Multimedia FFmpeg configure line: binary_provenance in manifest.json",
        "- Momento helper recipe: scripts/build_ffmpeg_helper.sh in the Momento source archive",
        "",
        "All files were accepted only after SHA-256 verification against manifest.json.",
        "The original archive formats are retained under sources/.",
        "",
        f"Included source archives: {len(sources)}",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def build_bundle(manifest: Path, output: Path, cache: Path, *, offline: bool) -> None:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    sources = _validate_manifest(data)
    cache.mkdir(parents=True, exist_ok=True)
    resolved = [_download(source, cache, offline=offline) for source in sources]

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    if partial.exists():
        partial.unlink()
    normalized_manifest = (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
        archive.writestr(_zip_info("README.txt"), _readme(data, sources))
        archive.writestr(_zip_info("manifest.json"), normalized_manifest)
        for source, path in zip(sources, resolved, strict=True):
            with archive.open(_zip_info(f"sources/{source['filename']}"), "w") as target:
                with path.open("rb") as handle:
                    shutil.copyfileobj(handle, target, length=CHUNK_SIZE)
    os.replace(partial, output)

    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"source bundle CRC failure: {bad}")
        for source in sources:
            digest = hashlib.sha256()
            with archive.open(f"sources/{source['filename']}") as handle:
                for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
            if digest.hexdigest() != source["sha256"]:
                raise RuntimeError(f"source bundle hash mismatch: {source['filename']}")

    print(f"PASS - {len(sources)} corresponding-source archives verified")
    print(f"PASS - deterministic source bundle: {output}")
    print(f"SHA256 - {sha256(output)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", type=Path, default=ROOT / "tmp" / "corresponding-sources")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    manifest = args.manifest.resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    output = args.output or ROOT / "dist" / "source" / data["bundle_name"]
    build_bundle(manifest, output.resolve(), args.cache.resolve(), offline=args.offline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
