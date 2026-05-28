"""User-data directory helpers (config, logs, etc.) under %APPDATA%/Momento."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Momento"


def appdata_dir() -> Path:
    """Return %APPDATA%/Momento, creating it if missing."""
    base = os.environ.get("APPDATA")
    if not base:
        # Fallback for non-Windows dev environments
        base = str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = appdata_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return appdata_dir() / "config.json"


def window_state_path() -> Path:
    """INI file QSettings writes editor geometry to — kept alongside the
    config so all user state lives under one folder."""
    return appdata_dir() / "window_state.ini"


def youtube_token_path() -> Path:
    """DPAPI-encrypted blob holding the user's YouTube OAuth refresh token.

    File contents are opaque ciphertext — bound to the current Windows user
    account, so copying the file to another machine won't help an attacker.
    """
    return appdata_dir() / "youtube_token.dat"


def youtube_avatar_path() -> Path:
    """Cached PNG of the connected channel's avatar, for the Settings chip.

    Non-sensitive — a public channel thumbnail. Written on connect, deleted
    on disconnect. Missing file just means "no avatar to show".
    """
    return appdata_dir() / "youtube_avatar.png"


def default_output_folder() -> Path:
    """Return the default folder path for recorded MP4 files.

    Does NOT create the folder — that would crash startup if the user's
    Videos drive happens to be unmounted. The Recorder creates the folder
    lazily when it's about to write into it.
    """
    return Path.home() / "Videos" / APP_NAME
