"""Ownership and in-flight activity guards for recording file mutations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from momento.core.game_names import game_slug_from_filename
from momento.core.recording_files import RECORDING_SUFFIXES, is_repair_temp
from momento.util.ffmpeg_path import ffprobe_exe

logger = logging.getLogger(__name__)

OWNERSHIP_SIDECAR_SUFFIX = ".momento.json"
_MARKER_SCHEMA = 1
_MARKER_OWNER = "Momento"
_MOMENTO_GAME_TAG = "MOMENTO_GAME"
_FINGERPRINT_SAMPLE_BYTES = 64 * 1024
_CREATION = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_marker_lock = threading.Lock()


def ownership_sidecar_path(path: Path | str) -> Path:
    media = Path(path)
    return media.with_name(media.name + OWNERSHIP_SIDECAR_SUFFIX)


def has_valid_ownership_marker(path: Path | str) -> bool:
    """Return whether the marker still describes this exact media file."""
    media = Path(path)
    marker = ownership_sidecar_path(media)
    try:
        stat = media.stat()
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    matches_identity = (
        isinstance(data, dict)
        and data.get("schema") == _MARKER_SCHEMA
        and data.get("owner") == _MARKER_OWNER
        and data.get("size") == stat.st_size
        and data.get("mtime_ns") == stat.st_mtime_ns
        and data.get("device") == stat.st_dev
        and data.get("file_id") == stat.st_ino
    )
    if not matches_identity:
        return False
    try:
        return data.get("fingerprint") == _media_fingerprint(media, stat.st_size)
    except OSError:
        return False


def mark_recording_owned(path: Path | str) -> bool:
    """Atomically bind a Momento ownership marker to the current file state."""
    media = Path(path)
    if (
        not media.is_file()
        or media.suffix.lower() not in RECORDING_SUFFIXES
        or is_repair_temp(media)
    ):
        return False
    marker = ownership_sidecar_path(media)
    tmp = marker.with_name(
        f".{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with _marker_lock:
        try:
            before = media.stat()
            fingerprint = _media_fingerprint(media, before.st_size)
            payload = {
                "schema": _MARKER_SCHEMA,
                "owner": _MARKER_OWNER,
                "size": before.st_size,
                "mtime_ns": before.st_mtime_ns,
                "device": before.st_dev,
                "file_id": before.st_ino,
                "fingerprint": fingerprint,
            }
            tmp.write_text(
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            after = media.stat()
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
            ):
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, marker)
            return True
        except OSError as exc:
            logger.warning("Could not write ownership marker for %s: %s", media, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False


def _media_fingerprint(path: Path, size: int) -> str:
    """Hash small samples from both ends without scanning multi-GB media."""
    digest = hashlib.sha256()
    digest.update(size.to_bytes(8, "little", signed=False))
    with path.open("rb") as fh:
        digest.update(fh.read(_FINGERPRINT_SAMPLE_BYTES))
        if size > _FINGERPRINT_SAMPLE_BYTES:
            fh.seek(max(0, size - _FINGERPRINT_SAMPLE_BYTES))
            digest.update(fh.read(_FINGERPRINT_SAMPLE_BYTES))
    return digest.hexdigest()


def is_momento_owned(path: Path | str) -> bool:
    """Conservatively identify media that Momento is allowed to auto-delete.

    A valid sidecar is the normal proof. Older standard-named recordings can
    migrate to that scheme only after ffprobe confirms their embedded
    ``MOMENTO_GAME`` tag. Renamed legacy files remain protected until the
    editor's normal metadata probe sees their tag and creates the marker.
    """
    media = Path(path)
    if has_valid_ownership_marker(media):
        return True
    if (
        not media.is_file()
        or media.suffix.lower() not in RECORDING_SUFFIXES
        or is_repair_temp(media)
        or not game_slug_from_filename(media.name)
    ):
        return False
    if not _has_embedded_momento_tag(media):
        return False
    # Never auto-delete from transient evidence alone. If the durable marker
    # cannot be written, leave the file untouched.
    return mark_recording_owned(media)


def _has_embedded_momento_tag(path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                str(ffprobe_exe()),
                "-v",
                "error",
                "-show_entries",
                f"format_tags={_MOMENTO_GAME_TAG}",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=_CREATION,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    wanted = f"TAG:{_MOMENTO_GAME_TAG}".casefold()
    for line in (result.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == wanted and value.strip():
            return True
    return False


_activity_lock = threading.Lock()
_active_paths: dict[str, int] = {}


def _path_key(path: Path | str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


class FileActivity:
    """Idempotent handle that keeps one or more media paths protected."""

    def __init__(self, paths: tuple[Path | str, ...]) -> None:
        self._keys = tuple(dict.fromkeys(_path_key(path) for path in paths))
        self._released = False
        self._release_lock = threading.Lock()
        with _activity_lock:
            for key in self._keys:
                _active_paths[key] = _active_paths.get(key, 0) + 1

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        with _activity_lock:
            for key in self._keys:
                remaining = _active_paths.get(key, 0) - 1
                if remaining > 0:
                    _active_paths[key] = remaining
                else:
                    _active_paths.pop(key, None)

    def __enter__(self) -> FileActivity:
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()


def begin_file_activity(*paths: Path | str) -> FileActivity:
    return FileActivity(paths)


def has_active_file_activity() -> bool:
    """Return whether any media mutation currently owns a file lease."""
    with _activity_lock:
        return bool(_active_paths)


def is_file_active(path: Path | str) -> bool:
    key = _path_key(path)
    with _activity_lock:
        if _active_paths.get(key, 0) > 0:
            return True
    # RepairJob owns a pre-existing thread-safe registry. Consult it lazily so
    # storage cleanup and the editor share one activity predicate without a
    # module-import cycle.
    from momento.core.media_probe import is_repairing

    return is_repairing(path)
