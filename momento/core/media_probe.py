"""Async ffprobe / ffmpeg helpers for recording metadata.

Used by the editor to:
  * Probe a recording's duration up front (separately from QMediaPlayer)
    so the scrubber + timeline aren't gated on Qt's WMF backend knowing
    the duration — which it doesn't, if the MKV's segment header wasn't
    finalised (i.e. the recording was killed before encoder.stop() ran).
  * Repair such recordings via ``ffmpeg -c copy`` which re-muxes them
    with proper Matroska segment + duration metadata.

Both run on the shared :class:`QThreadPool` from :mod:`momento.core.thumbnails`
so we don't spawn unbounded ffmpeg subprocesses.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from momento.core.thumbnails import _POOL  # reuse the shared thread pool
from momento.util.ffmpeg_path import ffmpeg_exe, ffprobe_exe

logger = logging.getLogger(__name__)

_CREATION = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Matroska tag the encoder stamps onto every recording so the editor can
# group by game even after the file is renamed. Defined here so the writer
# (encoder.py) and the reader (the probes below) reference the same key.
MOMENTO_GAME_TAG = "MOMENTO_GAME"


# ============================================================ duration probe

class _DurationSignals(QObject):
    # (path, seconds) — seconds < 0 means "unknown / broken metadata".
    done = pyqtSignal(str, float)


class DurationProbe(QRunnable):
    """Run ``ffprobe`` to get a recording's duration in seconds.

    Fast path: read ``format=duration`` directly, which is sub-50ms for any
    well-formed MKV/MP4. Returns ``-1.0`` if the value is missing or "N/A",
    which signals the caller that the file's segment header doesn't carry
    a duration (typical for a recording killed mid-write).
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        self.signals = _DurationSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            seconds = self._fast_probe()
        except Exception:
            logger.exception("DurationProbe crashed for %s", self._path)
            seconds = -1.0
        self.signals.done.emit(str(self._path), float(seconds))

    def _fast_probe(self) -> float:
        args = [
            str(ffprobe_exe()),
            "-v", "error",
            "-analyzeduration", "100M",
            "-probesize", "1G",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(self._path),
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=8,
                creationflags=_CREATION,
            )
        except (subprocess.TimeoutExpired, OSError):
            return -1.0
        if proc.returncode != 0:
            return -1.0
        text = (proc.stdout or "").strip()
        if not text or text.upper() == "N/A":
            return -1.0
        try:
            return float(text)
        except ValueError:
            return -1.0


def probe_duration_async(path: Path, on_done) -> DurationProbe:
    """Probe ``path``'s duration in background.

    Calls ``on_done(path_str, seconds)`` on the Qt main thread. ``seconds``
    is negative if the file lacks readable duration metadata.
    """
    job = DurationProbe(path)
    job.signals.done.connect(on_done)
    _POOL.start(job)
    return job


# =========================================================== metadata probe

class _MetadataSignals(QObject):
    # (path, duration_seconds, game_slug). duration < 0 means missing/N/A,
    # slug is empty when the MOMENTO_GAME tag isn't present.
    done = pyqtSignal(str, float, str)


class MetadataProbe(QRunnable):
    """One ffprobe call → duration + MOMENTO_GAME tag.

    Combining the two reads halves the number of subprocess spawns when
    the editor builds a folder listing, which matters for libraries with
    dozens of recordings.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        self.signals = _MetadataSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        duration = -1.0
        slug = ""
        try:
            args = [
                str(ffprobe_exe()),
                "-v", "error",
                "-analyzeduration", "100M",
                "-probesize", "1G",
                "-show_entries",
                f"format=duration:format_tags={MOMENTO_GAME_TAG}",
                "-of", "default=noprint_wrappers=1",
                str(self._path),
            ]
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=8,
                creationflags=_CREATION,
            )
            if proc.returncode == 0:
                tag_key = f"TAG:{MOMENTO_GAME_TAG}"
                for line in (proc.stdout or "").splitlines():
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if not val or val.upper() == "N/A":
                        continue
                    if key == "duration":
                        try:
                            duration = float(val)
                        except ValueError:
                            pass
                    elif key == tag_key:
                        slug = val
        except (subprocess.TimeoutExpired, OSError):
            pass
        except Exception:
            logger.exception("MetadataProbe crashed for %s", self._path)
        self.signals.done.emit(str(self._path), float(duration), slug)


def probe_metadata_async(path: Path, on_done) -> MetadataProbe:
    """Probe duration + MOMENTO_GAME tag in one shot.

    Calls ``on_done(path_str, duration_seconds, slug)`` on the Qt main
    thread. ``duration`` is negative when missing; ``slug`` is empty when
    the embedded tag is absent — caller falls back as appropriate.
    """
    job = MetadataProbe(path)
    job.signals.done.connect(on_done)
    _POOL.start(job)
    return job


# ================================================================== repair

class _RepairSignals(QObject):
    # (orig_path, ok, error_msg) — ok=True means original was replaced
    # in place with a re-muxed copy that carries proper metadata.
    done = pyqtSignal(str, bool, str)
    progress = pyqtSignal(str, float)  # path, seconds processed (best effort)


class RepairJob(QRunnable):
    """Re-mux a recording in place via ``ffmpeg -c copy``.

    Writes to ``<name>.repairing.mkv`` first, then atomically swaps over
    the original. The pre-swap original is kept as ``<name>.broken-bak.mkv``
    until the swap completes successfully, then unlinked. On any failure
    the original file is left untouched.

    Note: stream-copy. No re-encode → no quality loss → fast (limited by
    disk I/O, typically ~100-300 MB/s). For a broken file truncated mid-
    cluster, ffmpeg discards the trailing partial cluster.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path).resolve()
        self.signals = _RepairSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        src = self._path
        if not src.is_file():
            self.signals.done.emit(str(src), False, "File not found")
            return
        tmp = src.with_name(src.stem + ".repairing.mkv")
        backup = src.with_name(src.stem + ".broken-bak.mkv")
        # +genpts regenerates PTS for packets that lack them — common in
        # truncated MKVs. +igndts ignores corrupt DTS rather than aborting.
        args = [
            str(ffmpeg_exe()),
            "-hide_banner", "-loglevel", "error",
            "-y",
            "-fflags", "+genpts+igndts",
            "-i", str(src),
            "-c", "copy",
            "-map", "0",
            str(tmp),
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True,
                creationflags=_CREATION,
                timeout=1800,  # 30 min hard cap
            )
        except subprocess.TimeoutExpired:
            self._cleanup(tmp)
            self.signals.done.emit(str(src), False, "ffmpeg timed out (>30min)")
            return
        except OSError as e:
            self._cleanup(tmp)
            self.signals.done.emit(str(src), False, f"Could not run ffmpeg: {e}")
            return

        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 4096:
            self._cleanup(tmp)
            err = (proc.stderr or "").strip()[-400:] or f"rc={proc.returncode}"
            self.signals.done.emit(str(src), False, f"Repair failed: {err}")
            return

        # Atomic-ish swap: src→backup, tmp→src, unlink backup. If anything
        # blows up partway, the user still has a copy.
        try:
            if backup.exists():
                backup.unlink()
            src.rename(backup)
            tmp.rename(src)
        except OSError as e:
            # Best-effort recovery: try to put the backup back.
            try:
                if backup.exists() and not src.exists():
                    backup.rename(src)
            except OSError:
                pass
            self.signals.done.emit(str(src), False, f"File swap failed: {e}")
            return
        try:
            backup.unlink()
        except OSError:
            logger.warning("Repair succeeded but couldn't delete backup %s", backup)

        self.signals.done.emit(str(src), True, "")

    @staticmethod
    def _cleanup(tmp: Path) -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def repair_async(path: Path, on_done) -> RepairJob:
    """Re-mux ``path`` in place. ``on_done(path_str, ok, err)`` on Qt thread."""
    job = RepairJob(path)
    job.signals.done.connect(on_done)
    _POOL.start(job)
    return job


# ====================================================== recovery scan

def find_broken_recordings(
    folder: Path,
    skip_paths: set[Path] | None = None,
    min_age_seconds: float = 30.0,
    min_size_bytes: int = 1_048_576,
) -> list[Path]:
    """Find .mkv files in ``folder`` whose duration metadata is missing.

    Used at app startup to recover recordings left in a broken state by a
    previous crash (TerminateProcess, BSOD, power loss before
    ``encoder.stop()`` could run).

    Skips:
      * Files in ``skip_paths`` (e.g. the one currently being recorded).
      * Files modified within ``min_age_seconds`` (probably still open).
      * Files smaller than ``min_size_bytes`` (uninteresting test artefacts).

    Synchronous, but each probe runs in ~50 ms so a folder of dozens of
    recordings is still well under a second.
    """
    import time as _time
    skip = {p.resolve() for p in (skip_paths or ())}
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out: list[Path] = []
    now = _time.time()
    for p in folder.iterdir():
        if not p.is_file() or p.suffix.lower() != ".mkv":
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < min_size_bytes:
            continue
        if now - st.st_mtime < min_age_seconds:
            continue
        try:
            if p.resolve() in skip:
                continue
        except OSError:
            continue
        # Inline fast probe — same logic as DurationProbe._fast_probe but
        # synchronous, since the caller is willing to wait ~50ms × N.
        try:
            proc = subprocess.run(
                [
                    str(ffprobe_exe()), "-v", "error",
                    "-analyzeduration", "100M", "-probesize", "1G",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0", str(p),
                ],
                capture_output=True, text=True, timeout=5,
                creationflags=_CREATION,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode != 0:
            continue
        text = (proc.stdout or "").strip()
        if not text or text.upper() == "N/A":
            out.append(p)
    return out
