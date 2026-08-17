"""Locate bundled resources (known_games.json, icons, ...) in dev and frozen modes."""

from __future__ import annotations

import sys
from pathlib import Path


def resources_dir() -> Path:
    """Return the bundled resources/ directory."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is None:
            raise RuntimeError("Frozen build is missing sys._MEIPASS")
        return Path(meipass) / "resources"
    # momento/util/resources.py -> parents[2] is the repo root
    return Path(__file__).resolve().parents[2] / "resources"


def known_games_path() -> Path:
    return resources_dir() / "known_games.json"


def update_public_key_path() -> Path:
    """Return the Ed25519 public key used to authenticate release metadata."""
    path = resources_dir() / "update_public_key.pem"
    if not path.is_file():
        raise RuntimeError("Momento's update verification key is missing")
    return path


def icons_dir() -> Path:
    return resources_dir() / "icons"


def app_icon_path() -> Path | None:
    """Path to the multi-resolution app icon, or None if missing (dev tree)."""
    p = icons_dir() / "momento.ico"
    return p if p.is_file() else None


def sounds_dir() -> Path:
    return resources_dir() / "sounds"


def bookmark_sound_path() -> Path | None:
    """Path to the bookmark chime WAV, or None if missing."""
    p = sounds_dir() / "bookmark.wav"
    return p if p.is_file() else None


def youtube_dir() -> Path:
    return resources_dir() / "youtube"


def youtube_client_secrets_path() -> Path | None:
    """Return the optional source-only OAuth fallback, never one in a build.

    Kept for compatibility with older internal callers. Public frozen builds
    must configure YouTube through the user's DPAPI-protected AppData import.
    """
    if getattr(sys, "frozen", False):
        return None
    path = youtube_dir() / "client_secrets.json"
    return path if path.is_file() else None


def youtube_upload_available() -> bool:
    """Return whether this Momento version supports YouTube upload.

    Setup state is deliberately separate: upload controls remain available so
    an unconfigured user can open the setup guide.
    """
    return True
