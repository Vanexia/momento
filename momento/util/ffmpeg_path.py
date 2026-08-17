"""Locate the bundled ffmpeg.exe in both dev and PyInstaller-frozen modes."""

from __future__ import annotations

import sys
from pathlib import Path


def _repo_root() -> Path:
    # momento/util/ffmpeg_path.py -> parents[2] is the repo root
    return Path(__file__).resolve().parents[2]


def _frozen_root() -> Path:
    # PyInstaller sets sys._MEIPASS to the extraction dir at runtime
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:
        raise RuntimeError("ffmpeg_path._frozen_root called outside a frozen build")
    return Path(meipass)


def ffmpeg_exe() -> Path:
    """Return absolute path to the bundled ffmpeg.exe."""
    base = _frozen_root() if getattr(sys, "frozen", False) else _repo_root()
    path = base / "resources" / "ffmpeg" / "ffmpeg.exe"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled ffmpeg.exe not found at {path}")
    return path


def ffprobe_exe() -> Path:
    """Return absolute path to the bundled ffprobe.exe."""
    base = _frozen_root() if getattr(sys, "frozen", False) else _repo_root()
    path = base / "resources" / "ffmpeg" / "ffprobe.exe"
    if not path.is_file():
        raise FileNotFoundError(f"Bundled ffprobe.exe not found at {path}")
    return path
