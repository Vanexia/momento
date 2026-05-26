"""Cached single-frame thumbnails for MP4 recordings.

For each MP4 we extract one JPEG frame (small, ~320 px wide) and cache it as
``<name>.thumb.jpg`` next to the file. Re-used until the source mtime is newer
than the cache mtime (i.e. file got rewritten).

Extraction runs on a shared :class:`QThreadPool` (capped concurrency) so we
don't end up with 20+ ffmpeg subprocesses fighting for I/O when the user has
a big recordings folder. We use ``QRunnable`` rather than QThread because the
auto-managed lifecycle avoids the Python-GC vs Qt-object pitfalls that plague
manual QThread management.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from momento.util.ffmpeg_path import ffmpeg_exe

logger = logging.getLogger(__name__)

THUMB_SUFFIX = ".thumb.jpg"
THUMB_WIDTH = 320  # px; height auto from aspect ratio


def thumb_path_for(mp4_path: Path | str) -> Path:
    p = Path(mp4_path)
    return p.with_name(p.name + THUMB_SUFFIX)


def thumb_is_fresh(mp4_path: Path) -> bool:
    """True if a cached thumbnail exists and is newer than the source MP4."""
    tp = thumb_path_for(mp4_path)
    if not tp.is_file():
        return False
    try:
        return tp.stat().st_mtime >= mp4_path.stat().st_mtime
    except OSError:
        return False


def _extract(mp4_path: Path, when_seconds: float = 1.0) -> Path | None:
    """Synchronous frame extraction. Returns the thumb path on success.

    Picks the frame at ``when_seconds`` if the clip is long enough, otherwise
    falls back to the first available frame. -ss before -i = fast seek.
    """
    tp = thumb_path_for(mp4_path)
    ff = ffmpeg_exe()
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    last_err = ""
    for ss in (when_seconds, 0.0):
        args = [
            str(ff),
            "-hide_banner", "-loglevel", "error",
            "-y",
            "-ss", f"{ss:.3f}",
            "-i", str(mp4_path),
            "-frames:v", "1",
            "-vf", f"scale={THUMB_WIDTH}:-2",
            "-q:v", "5",  # JPEG quality (2=best, 31=worst)
            str(tp),
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, check=False,
                creationflags=creationflags, timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            last_err = str(e)
            continue
        if proc.returncode == 0 and tp.exists() and tp.stat().st_size > 0:
            return tp
        last_err = (proc.stderr or "").strip() or f"rc={proc.returncode}"
    logger.warning("Thumbnail extraction failed for %s: %s", mp4_path.name, last_err[:200])
    return None


# Shared thread pool — capped to 2 concurrent ffmpeg subprocesses to avoid
# I/O thrash and the memory cost of many ffmpeg.exe copies (each ~217 MB).
_POOL = QThreadPool.globalInstance()
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
            logger.exception("ThumbnailJob crashed for %s", self._path)
            self.signals.done.emit(str(self._path), "")


def extract_async(mp4_path: Path, on_done) -> ThumbnailJob:
    """Submit a thumbnail extraction job. ``on_done(path_str, thumb_path_str)``
    fires on the Qt main thread when finished (thumb path is "" on failure)."""
    job = ThumbnailJob(mp4_path)
    job.signals.done.connect(on_done)
    _POOL.start(job)
    return job
