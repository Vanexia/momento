"""Mementos data locations, retaining existing Momento profiles in place."""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from ctypes.wintypes import DWORD, HANDLE
from pathlib import Path

APP_NAME = "Mementos"
LEGACY_APP_NAME = "Momento"


def _roaming_base() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _has_state(directory: Path, *, ignored: tuple[str, ...] = ()) -> bool:
    return directory.is_dir() and any(
        item.name not in ignored for item in directory.iterdir()
    )


def appdata_dir() -> Path:
    """Use the new name for fresh profiles; never abandon existing user data."""
    base = _roaming_base()
    path = base / APP_NAME
    legacy = base / LEGACY_APP_NAME
    # A startup can create logs before it acquires the instance lock. Neither
    # those logs nor an empty folder should shadow an established profile.
    ignored = ("logs", "momento.lock")
    if not _has_state(path, ignored=ignored) and _has_state(legacy, ignored=ignored):
        path = legacy
    path.mkdir(parents=True, exist_ok=True)
    return path


def instance_lock_path() -> Path:
    """Share the original file lock so old and renamed apps cannot both record."""
    return _roaming_base() / LEGACY_APP_NAME / "momento.lock"


def logs_dir() -> Path:
    path = appdata_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def update_cache_dir() -> Path:
    """Return the local, non-roaming cache used for verified update files."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    path = Path(base) / APP_NAME / "updates"
    legacy = Path(base) / LEGACY_APP_NAME / "updates"
    # Preserve existing trust and pending attempts. Once the new cache contains
    # state, an old executable creating its cache must not reset that history.
    # Corrupt state still counts: UpdateCache must reject it, not bypass it.
    if not _has_state(path) and _has_state(legacy):
        path = legacy
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


def youtube_oauth_client_path() -> Path:
    """DPAPI-protected user-imported Google Desktop OAuth client."""
    return appdata_dir() / "youtube_oauth_client.dat"


def youtube_avatar_path() -> Path:
    """Cached PNG of the connected channel's avatar, for the Settings chip.

    Non-sensitive — a public channel thumbnail. Written on connect, deleted
    on disconnect. Missing file just means "no avatar to show".
    """
    return appdata_dir() / "youtube_avatar.png"


def default_output_folder() -> Path:
    """Return a Mementos folder beneath the Windows Videos known folder.

    Does NOT create the folder — that would crash startup if the user's
    Videos drive happens to be unmounted. The Recorder creates the folder
    lazily when it's about to write into it.
    """
    videos = _windows_videos_folder() if sys.platform == "win32" else None
    return (videos or (Path.home() / "Videos")) / APP_NAME


def _windows_videos_folder() -> Path | None:
    """Resolve FOLDERID_Videos, including OneDrive and other redirections."""
    class GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", DWORD),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        )

        @classmethod
        def from_uuid(cls, value: uuid.UUID) -> "GUID":
            raw = value.bytes_le
            return cls(
                int.from_bytes(raw[0:4], "little"),
                int.from_bytes(raw[4:6], "little"),
                int.from_bytes(raw[6:8], "little"),
                (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:]),
            )

    try:
        folder_id = GUID.from_uuid(uuid.UUID("18989b1d-99b5-455b-841c-ab7c74e4ddfc"))
        value = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, HANDLE(0), ctypes.byref(value)
        )
        if result != 0 or not value.value:
            return None
        try:
            return Path(value.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(value)
    except (AttributeError, OSError, ValueError):
        return None
