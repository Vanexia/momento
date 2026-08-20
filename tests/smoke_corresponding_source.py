"""Static and archive checks for third-party corresponding source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "build" / "corresponding_sources.json"
HASH = re.compile(r"[0-9a-f]{64}")
EXPECTED_ROLES = {
    "helper": 1,
    "python-runtime": 3,
    "pyav-runtime-input": 5,
    "windows-runtime": 2,
    "qt-runtime": 4,
    "qt-multimedia-input": 2,
}


def _digest(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def main(archive_path: Path | None = None) -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sources = data["sources"]
    failures: list[str] = []

    if data.get("schema_version") != 1 or data.get("release") != "0.2.6":
        failures.append("manifest schema or release is not current")
    filenames = [entry["filename"] for entry in sources]
    if len(filenames) != len(set(filenames)):
        failures.append("source filenames are not unique")
    for entry in sources:
        if Path(entry["filename"]).name != entry["filename"]:
            failures.append(f"unsafe filename: {entry['filename']}")
        if not HASH.fullmatch(entry["sha256"]):
            failures.append(f"invalid SHA-256: {entry['filename']}")
        if not entry["url"].startswith("https://"):
            failures.append(f"non-HTTPS source: {entry['filename']}")
    for role, count in EXPECTED_ROLES.items():
        actual = sum(entry["role"] == role for entry in sources)
        if actual != count:
            failures.append(f"{role} has {actual} entries, expected {count}")

    lock = (ROOT / "requirements-release-hashed.txt").read_text(encoding="utf-8")
    for key in ("pyqt6_wheel", "pyqt6_qt6_wheel", "pyqt6_sip_wheel"):
        wheel = data["binary_provenance"].get(key, {})
        if wheel.get("sha256") not in lock:
            failures.append(f"{key} hash does not match the release wheel lock")

    pyav_contract = json.loads(
        (ROOT / "build" / "pyav_runtime.json").read_text(encoding="utf-8")
    )
    pyav_provenance = data["binary_provenance"].get("pyav_wheel", {})
    if any(
        pyav_provenance.get(key) != pyav_contract["artifact"].get(key)
        for key in ("filename", "sha256")
    ):
        failures.append("custom PyAV wheel provenance differs from its runtime contract")
    source_by_filename = {entry["filename"]: entry for entry in sources}
    for source in pyav_contract["sources"]:
        declared = source_by_filename.get(source["filename"], {})
        if any(declared.get(key) != source.get(key) for key in ("url", "sha256")):
            failures.append(
                f"custom PyAV runtime source differs from its contract: {source['filename']}"
            )

    qt_configure = data["binary_provenance"].get(
        "qt_multimedia_ffmpeg_configure", ""
    )
    for required in ("--toolchain=msvc", "--enable-shared", "--enable-zlib"):
        if required not in qt_configure:
            failures.append(f"Qt Multimedia FFmpeg provenance omits {required}")

    helper = next((entry for entry in sources if entry["role"] == "helper"), None)
    helper_recipe = (ROOT / "scripts" / "build_ffmpeg_helper.sh").read_text(encoding="utf-8")
    if not helper or helper["sha256"] not in helper_recipe:
        failures.append("helper recipe and source manifest do not share the source hash")
    for recipe in ("runtime_manifest", "build_recipe", "native_build_recipe"):
        relative = pyav_provenance.get(recipe, "")
        if not relative or not (ROOT / relative).is_file():
            failures.append(f"custom PyAV provenance omits its tracked {recipe}")

    if archive_path:
        expected_names = {"README.txt", "manifest.json"} | {
            f"sources/{filename}" for filename in filenames
        }
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if names != expected_names:
                failures.append("source bundle contents do not exactly match the manifest")
            if archive.testzip():
                failures.append("source bundle failed its ZIP CRC check")
            bundled_manifest = json.loads(archive.read("manifest.json"))
            if bundled_manifest != data:
                failures.append("bundled manifest differs from the tracked manifest")
            for entry in sources:
                name = f"sources/{entry['filename']}"
                if name in names:
                    with archive.open(name) as handle:
                        if _digest(handle) != entry["sha256"]:
                            failures.append(f"bundled hash mismatch: {entry['filename']}")

    if failures:
        for failure in failures:
            print(f"FAIL - {failure}")
        return 1
    print(f"PASS - {len(sources)} exact corresponding-source inputs are pinned")
    print("PASS - source roles, URLs, hashes, and build provenance are coherent")
    if archive_path:
        print("PASS - source bundle contents and every embedded hash are exact")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", nargs="?", type=Path)
    raise SystemExit(main(parser.parse_args().archive))
