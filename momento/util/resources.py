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
    """Path to the Google OAuth client_secrets.json shipped with Momento.

    This is the app's *identity* to Google for the Desktop OAuth flow —
    not a user secret. Google's docs explicitly call out that embedding
    desktop-app client credentials in a distributed binary is the supported
    pattern (the auth itself happens in the user's browser, not in the app).

    Returns None if the file isn't present — the YouTube UI surfaces a clear
    "this build wasn't shipped with YouTube credentials" message rather than
    crashing at first auth attempt.
    """
    p = youtube_dir() / "client_secrets.json"
    return p if p.is_file() else None
