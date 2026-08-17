"""Cached single-frame thumbnails for recordings (MKV + MP4 clips).

For each recording we extract one JPEG frame (small, ~320 px wide) and cache it
as ``<name>.thumb.jpg`` next to the file. Re-used until the source mtime is
newer than the cache mtime (i.e. file got rewritten).

Frame choice: the MIDDLE of the recording (YouTube-style), falling back to 25%
then near-the-start. Game recordings open on seconds of loading-screen black,
so the old grab-the-frame-at-1s produced a library of black rectangles. On top
of the seek, ffmpeg's ``thumbnail`` filter scans a small batch of frames from
the seek point and picks the most representative one, so landing on a mid-fade
black frame still yields real content.

Extraction runs on a shared :class:`QThreadPool` (capped concurrency) so we
don't end up with 20+ ffmpeg subprocesses fighting for I/O when the user has
a big recordings folder. We use ``QRunnable`` rather than QThread because the
auto-managed lifecycle avoids the Python-GC vs Qt-object pitfalls that plague
manual QThread management.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from momento.util.ffmpeg_path import ffmpeg_exe, ffprobe_exe

logger = logging.getLogger(__name__)

THUMB_SUFFIX = ".thumb.jpg"
THUMB_WIDTH = 320  # px; height auto from aspect ratio
# How many frames the ffmpeg `thumbnail` filter scans (from the seek point)
# to pick the most representative one. 30 frames = 0.5s at 60 fps: cheap to
# decode, enough to skip past a black frame the seek happened to land on.
_THUMB_SCAN_FRAMES = 30


def thumb_path_for(mp4_path: Path | str) -> Path:
    p = Path(mp4_path)
    return p.with_name(p.name + THUMB_SUFFIX)


def thumb_is_fresh(mp4_path: Path) -> bool:
    """True if a cached thumbnail exists, has content, and is newer than the
    source file. A zero-byte thumb (aborted extraction) must not count as
    fresh — it would render as a broken image forever."""
    tp = thumb_path_for(mp4_path)
    if not tp.is_file():
        return False
    try:
        st = tp.stat()
        return st.st_size > 0 and st.st_mtime >= mp4_path.stat().st_mtime
    except OSError:
        return False


def _probe_duration_seconds(media_path: Path) -> float:
    """Container duration via ffprobe; 0.0 when unknown (unfinalised MKV)."""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [
                str(ffprobe_exe()),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(media_path),
            ],
            capture_output=True, text=True, check=False,
            creationflags=creationflags, timeout=15,
        )
        return max(0.0, float((proc.stdout or "").strip()))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0.0


def _extract(media_path: Path) -> Path | None:
    """Synchronous frame extraction. Returns the thumb path on success.

    Seeks to the middle of the recording (then 25%, then near-start as
    fallbacks — a broken index can defeat the deeper seeks) and picks a
    representative frame from a short scan there. -ss before -i = fast seek.
    """
    tp = thumb_path_for(media_path)
    ff = ffmpeg_exe()
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    duration = _probe_duration_seconds(media_path)
    seeks: list[float] = []
    if duration > 0:
        for frac in (0.5, 0.25):
            ss = duration * frac
            if ss > 1.0 and ss not in seeks:
                seeks.append(ss)
    seeks += [1.0, 0.0]

    last_err = ""
    for ss in seeks:
        args = [
            str(ff),
            "-hide_banner", "-loglevel", "error",
            "-y",
            "-ss", f"{ss:.3f}",
            "-i", str(media_path),
            "-frames:v", "1",
            "-vf", f"thumbnail={_THUMB_SCAN_FRAMES},scale={THUMB_WIDTH}:-2",
            "-q:v", "5",  # JPEG quality (2=best, 31=worst)
            str(tp),
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, check=False,
                creationflags=creationflags, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            last_err = str(e)
            continue
        if proc.returncode == 0 and tp.exists() and tp.stat().st_size > 0:
            return tp
        last_err = (proc.stderr or "").strip() or f"rc={proc.returncode}"
    # Every candidate failed — don't leave a partial/zero-byte file behind:
    # thumb_is_fresh would treat it as a valid cached thumb forever.
    with contextlib.suppress(OSError):
        tp.unlink(missing_ok=True)
    logger.warning("Thumbnail extraction failed: %s", last_err[:200])
    return None


# Dedicated pool for thumbnail extraction, capped to 2 concurrent ffmpeg
# subprocesses (I/O thrash and concurrent decode work add up on large libraries).
# Deliberately NOT QThreadPool.globalInstance(): capping the global pool to 2
# used to throttle every OTHER global-pool user — duration/metadata probes,
# the startup auto-REPAIR of crash-broken recordings, the settings avatar
# fetch — behind thumbnail jobs. A new or cache-wiped library queues dozens of
# extractions at once; a crash-recovery repair must never wait behind them.
_POOL = QThreadPool()
_POOL.setMaxThreadCount(2)


class _ThumbSignals(QObject):
    done = pyqtSignal(str, str)  # mp4 abs path, thumb abs path ("" on fail)


class ThumbnailJob(QRunnable):
    """QRunnable that extracts one thumbnail. Submit via :func:`extract_async`.

    Auto-deleted by the thread pool after run(); no manual lifecycle to wrangle.
    """

    def __init__(self, mp4_path: Path) -> None:
        super().__init__()
        self._path = mp4_path
        self.signals = _ThumbSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            tp = _extract(self._path)
            self.signals.done.emit(str(self._path), str(tp) if tp else "")
        except Exception:
            logger.exception("ThumbnailJob crashed")
            self.signals.done.emit(str(self._path), "")


# Paths with an extraction currently queued or running. Dedupes submissions at
# the source: the editor's refresh() rebuilds its own "submitted" tracking, so
# without this a refresh during a large backlog re-submits queued paths and two
# ffmpeg processes race `-y`-writing the same .thumb.jpg.
_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def extract_async(mp4_path: Path, on_done) -> ThumbnailJob | None:
    """Submit a thumbnail extraction job. ``on_done(path_str, thumb_path_str)``
    fires on the Qt main thread when finished (thumb path is "" on failure).
    Returns None without queueing if an extraction for the same path is
    already queued or running."""
    key = str(Path(mp4_path))
    with _in_flight_lock:
        if key in _in_flight:
            return None
        _in_flight.add(key)
    job = ThumbnailJob(mp4_path)

    def _deregister(_p: str, _tp: str, _key: str = key) -> None:
        with _in_flight_lock:
            _in_flight.discard(_key)

    # Connection order is call order: deregister first, then the caller's slot
    # (so a re-kick from inside on_done isn't refused as still-in-flight).
    job.signals.done.connect(_deregister)
    job.signals.done.connect(on_done)
    _POOL.start(job)
    return job
