"""Verify the bundled minimal FFmpeg helper's release contract."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pefile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.util.ffmpeg_path import ffmpeg_exe, ffprobe_exe  # noqa: E402


def _run(args: list[str]) -> str:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def _imports(path: Path) -> set[str]:
    image = pefile.PE(str(path), fast_load=True)
    try:
        image.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        return {
            entry.dll.decode("ascii", errors="replace").casefold()
            for entry in getattr(image, "DIRECTORY_ENTRY_IMPORT", ())
        }
    finally:
        image.close()


def _is_windows_system_dll(name: str) -> bool:
    return (
        name in {"bcrypt.dll", "kernel32.dll", "shell32.dll"}
        or name.startswith("api-ms-win-")
        or name.startswith("ext-ms-win-")
    )


def main(helper_dir: Path | None = None) -> int:
    ffmpeg = helper_dir / "ffmpeg.exe" if helper_dir else ffmpeg_exe()
    ffprobe = helper_dir / "ffprobe.exe" if helper_dir else ffprobe_exe()
    failures: list[str] = []

    for tool in (ffmpeg, ffprobe):
        if not tool.is_file():
            failures.append(f"missing helper: {tool}")
            continue
        version = _run([str(tool), "-hide_banner", "-version"])
        first_line = version.splitlines()[0] if version else ""
        if "8.1.2-momento-" not in first_line:
            failures.append(f"unexpected version identity: {first_line}")
        imports = _imports(tool)
        external = sorted(name for name in imports if not _is_windows_system_dll(name))
        if external:
            failures.append(f"{tool.name} has non-system DLL dependencies: {external}")

    if not failures:
        formats = _run([str(ffmpeg), "-hide_banner", "-formats"])
        codecs = _run([str(ffmpeg), "-hide_banner", "-codecs"])
        filters = _run([str(ffmpeg), "-hide_banner", "-filters"])
        protocols = _run([str(ffmpeg), "-hide_banner", "-protocols"])
        build = _run([str(ffmpeg), "-hide_banner", "-buildconf"])

        for term in ("matroska", "mov", "mp4", "image2"):
            if term not in formats:
                failures.append(f"missing format: {term}")
        for term in ("h264", "mjpeg"):
            if term not in codecs:
                failures.append(f"missing codec: {term}")
        for term in ("thumbnail", "scale"):
            if term not in filters:
                failures.append(f"missing filter: {term}")
        if "file" not in protocols:
            failures.append("missing file protocol")
        for forbidden in (
            "http",
            "https",
            "tcp",
            "udp",
            "h264_nvenc",
            "h264_amf",
            "h264_qsv",
            "libx264",
            "ddagrab",
            "lavfi",
        ):
            if forbidden in "\n".join((formats, codecs, filters, protocols)):
                failures.append(f"unexpected helper capability: {forbidden}")
        for option in ("--disable-network", "--disable-autodetect", "--extra-ldflags=-static"):
            if option not in build:
                failures.append(f"missing build restriction: {option}")

    if failures:
        for failure in failures:
            print(f"FAIL - {failure}")
        return 1
    print("PASS - minimal FFmpeg helper identity and capability allowlist")
    print("PASS - helper imports only Windows system DLLs")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("helper_dir", nargs="?", type=Path)
    raise SystemExit(main(parser.parse_args().helper_dir))
