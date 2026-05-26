"""M1 verification: run bundled ffmpeg -encoders and confirm h264_nvenc is present.

Usage:
    .venv\\Scripts\\python.exe tests\\check_ffmpeg.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Allow running this file directly without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.util.ffmpeg_path import ffmpeg_exe  # noqa: E402

REQUIRED_ENCODERS = ("h264_nvenc",)
REQUIRED_FILTERS = ("ddagrab", "amix", "volume")


def _run(args: list[str]) -> str:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def main() -> int:
    ff = ffmpeg_exe()
    print(f"Using ffmpeg: {ff}")

    version_line = _run([str(ff), "-hide_banner", "-version"]).splitlines()[0]
    print(version_line)

    encoders = _run([str(ff), "-hide_banner", "-encoders"])
    filters = _run([str(ff), "-hide_banner", "-filters"])

    missing: list[str] = []
    for enc in REQUIRED_ENCODERS:
        if enc not in encoders:
            missing.append(f"encoder:{enc}")
        else:
            print(f"OK encoder: {enc}")

    for flt in REQUIRED_FILTERS:
        if flt not in filters:
            missing.append(f"filter:{flt}")
        else:
            print(f"OK filter:  {flt}")

    if missing:
        print("\nMISSING capabilities:")
        for m in missing:
            print(f"  - {m}")
        return 1

    print("\nAll required ffmpeg capabilities present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
