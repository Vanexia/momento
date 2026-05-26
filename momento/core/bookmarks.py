"""Per-recording bookmark persistence.

Each recording has an optional sidecar JSON ``<name>.<ext>.bookmarks.json``
next to it (e.g. ``foo.mkv.bookmarks.json``):

    {"version": 1, "bookmarks": [12.5, 47.0, 113.2]}

Timestamps are seconds from the start of the recording (float). The list is
kept sorted ascending; duplicates within 0.5s are merged so a fat-fingered
double-tap on the hotkey doesn't create twin entries. The recording's full
filename (including extension) is part of the sidecar name so .mkv and a
hypothetical same-stem .mp4 don't collide.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".bookmarks.json"
_MIN_DEDUPE_GAP = 0.5  # seconds


def sidecar_path_for(recording_path: Path | str) -> Path:
    p = Path(recording_path)
    return p.with_name(p.name + SIDECAR_SUFFIX)


def load_bookmarks(recording_path: Path | str) -> list[float]:
    path = sidecar_path_for(recording_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Bookmark file %s is unreadable; ignoring", path)
        return []
    raw = data.get("bookmarks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[float] = []
    for v in raw:
        try:
            out.append(max(0.0, float(v)))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def save_bookmarks(recording_path: Path | str, bookmarks: list[float]) -> None:
    path = sidecar_path_for(recording_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "bookmarks": sorted(max(0.0, float(b)) for b in bookmarks)}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class BookmarkStore:
    """In-memory bookmark list for the *current* recording.

    Created by SessionManager at the start of each recording, persisted to disk
    incrementally on each add() so a crash doesn't lose them.
    """

    def __init__(self, recording_path: Path | str) -> None:
        self._path = Path(recording_path)
        self._sidecar = sidecar_path_for(self._path)
        self._items: list[float] = []
        self._lock = threading.Lock()

    @property
    def recording_path(self) -> Path:
        return self._path

    def add(self, seconds: float) -> bool:
        """Add a timestamp; returns False if it was deduped."""
        seconds = max(0.0, float(seconds))
        with self._lock:
            for existing in self._items:
                if abs(existing - seconds) < _MIN_DEDUPE_GAP:
                    return False
            self._items.append(seconds)
            self._items.sort()
            snapshot = list(self._items)
        try:
            save_bookmarks(self._path, snapshot)
        except OSError:
            logger.exception("Could not write bookmarks sidecar for %s", self._path)
        return True

    def snapshot(self) -> list[float]:
        with self._lock:
            return list(self._items)
