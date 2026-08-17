"""Async ffprobe / ffmpeg helpers for recording metadata.

Used by the editor to:
  * Probe a recording's duration up front (separately from QMediaPlayer)
    so the scrubber + timeline aren't gated on Qt's WMF backend knowing
    the duration — which it doesn't, if the MKV's segment header wasn't
    finalised (i.e. the recording was killed before encoder.stop() ran).
  * Repair such recordings via ``ffmpeg -c copy`` which re-muxes them
    with proper Matroska segment + duration metadata.

Both run on this module's own bounded :class:`QThreadPool` (2 threads) so we
don't spawn unbounded ffmpeg subprocesses — and so a probe or a startup
auto-repair never queues behind a library-wide thumbnail regeneration (the
thumbnail pool is separate; see :mod:`momento.core.thumbnails`).
"""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from momento.core.recording_files import (
    REPAIR_TMP_SUFFIX,
    is_repair_temp,
    recording_path_for_repair_temp,
)
from momento.core.media_validation import validate_repair_candidate
from momento.core.recording_safety import (
    has_valid_ownership_marker,
    is_momento_owned,
    mark_recording_owned,
)
from momento.util.ffmpeg_path import ffmpeg_exe, ffprobe_exe

logger = logging.getLogger(__name__)

_CREATION = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Dedicated pool for probe + repair jobs, capped to 2 concurrent subprocesses.
# Separate from the thumbnail pool so a duration probe or a startup
# auto-repair of a crash-broken recording never queues behind dozens of
# thumbnail extractions.
_POOL = QThreadPool()
_POOL.setMaxThreadCount(2)

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
            logger.exception("DurationProbe crashed")
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
            logger.exception("MetadataProbe crashed")
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


# The swap at the end of a repair must overwrite the original ``.mkv`` while
# something else may still hold a fleeting read handle on it — antivirus, the
# Windows search indexer, or one of the editor's own metadata-probe/thumbnail
# jobs that opened the file the instant before it finished. On Windows such a
# handle (opened without ``FILE_SHARE_DELETE``) makes the replace fail: a
# handle on the *source* temp gives a sharing violation (``WinError 32``); a
# handle on the *destination* original gives access-denied (``WinError 5``).
# Both clear when the reader closes, so we retry with backoff — totalling
# ~9.9 s, enough to outlast a metadata probe (ffprobe, 8 s cap) or a thumbnail
# extraction holding the original. Permanent errors fail fast (see below).
_SWAP_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.7) + (1.0,) * 8


def _is_transient_lock_error(e: OSError) -> bool:
    """True if waiting could plausibly let ``os.replace`` succeed later.

    A locked file (another process holds a non-delete-shared handle) clears on
    its own; anything else — a missing temp, a cross-device move, a path that
    became a directory — never does, so the caller should give up immediately
    rather than sleep out the whole budget.
    """
    winerror = getattr(e, "winerror", None)
    if winerror is not None:  # Windows
        return winerror in (5, 32)  # ACCESS_DENIED / SHARING_VIOLATION
    return e.errno in (errno.EACCES, errno.EBUSY)  # POSIX best-effort


def _replace_with_retry(tmp: Path, dst: Path) -> OSError | None:
    """``os.replace(tmp, dst)`` retried with backoff on transient locks.

    Returns ``None`` on success or the last :class:`OSError` if every attempt
    failed (or the first non-transient error). ``os.replace`` is atomic on
    NTFS, so ``dst`` is at every instant either the untouched original or the
    fully-swapped repaired file — there is no partial-write window, which is
    why no separate backup copy is needed. The retries only exist to ride out
    the transient lock described above; permanent errors return at once.
    """
    last: OSError | None = None
    for i, delay in enumerate((0.0, *_SWAP_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, dst)
            return None
        except OSError as e:
            last = e
            if not _is_transient_lock_error(e):
                return e  # waiting can't help — give up now
            if i == 0:
                logger.info("Repair swap blocked; retrying: %s", e)
    return last


class RepairJob(QRunnable):
    """Re-mux a recording in place via ``ffmpeg -c copy``.

    Writes to ``<name>.repairing.mkv`` first, then atomically replaces the
    original with :func:`_replace_with_retry` (``os.replace`` + backoff). The
    replace is atomic, so on any failure — including ffmpeg failing or every
    swap attempt being blocked — the original file is left untouched and the
    temp is cleaned up.

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
        try:
            self._run()
        except Exception as exc:
            # QRunnable exceptions do not have a caller that can recover. A
            # missing terminal signal would also leave repair_async's registry
            # entry and the editor's progress state stuck forever.
            logger.exception("Unexpected repair failure")
            tmp = self._path.with_name(self._path.stem + REPAIR_TMP_SUFFIX)
            self._cleanup(tmp)
            self.signals.done.emit(
                str(self._path), False, f"Unexpected repair error: {exc}"
            )

    def _run(self) -> None:
        src = self._path
        if src.suffix.lower() != ".mkv":
            self.signals.done.emit(
                str(src), False, "Only MKV recordings can be repaired"
            )
            return
        if not src.is_file():
            self.signals.done.emit(str(src), False, "File not found")
            return
        # Never re-mux a repair work file — that would chain temps
        # (``X.repairing.repairing.mkv``) and never converge.
        if is_repair_temp(src):
            self.signals.done.emit(str(src), False, "Refusing to repair a temp file")
            return
        tmp = src.with_name(src.stem + REPAIR_TMP_SUFFIX)
        was_owned = has_valid_ownership_marker(src)
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

        validation_error = validate_repair_candidate(src, tmp)
        if validation_error is not None:
            self._cleanup(tmp)
            self.signals.done.emit(
                str(src), False, f"Repair validation failed: {validation_error}"
            )
            return

        # Atomic swap with retry. os.replace leaves ``src`` untouched on
        # failure, so the broken original is never lost.
        err = _replace_with_retry(tmp, src)
        if err is not None:
            self._cleanup(tmp)
            self.signals.done.emit(str(src), False, f"File swap failed: {err}")
            return

        if was_owned and not mark_recording_owned(src):
            logger.warning("Could not refresh ownership marker after repair")

        self.signals.done.emit(str(src), True, "")

    @staticmethod
    def _cleanup(tmp: Path) -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# Paths (resolved, as str) with a RepairJob queued or running. Lets callers
# avoid (a) queueing a second repair for the same file — two ffmpeg writers
# would clobber the one shared temp and the loser could os.replace garbage
# over the original — and (b) opening a file (ffprobe/thumbnail) while its
# repair is mid-swap, which is the read handle that defeats the swap. All
# mutations happen on the Qt thread (repair_async and the queued done slot),
# but the lock keeps ``is_repairing`` honest if ever read from a worker.
_in_flight_repairs: set[str] = set()
_repair_lock = threading.Lock()


def is_repairing(path: Path | str) -> bool:
    """True if a repair is queued or running for ``path``."""
    try:
        key = str(Path(path).resolve())
    except OSError:
        key = str(path)
    with _repair_lock:
        return key in _in_flight_repairs


def has_in_flight_repairs() -> bool:
    """Return whether any queued or running repair still owns a path."""
    with _repair_lock:
        return bool(_in_flight_repairs)


def repair_async(path: Path, on_done) -> RepairJob | None:
    """Re-mux ``path`` in place. ``on_done(path_str, ok, err)`` on Qt thread.

    Returns ``None`` without queueing for non-MKV inputs or if a repair for the
    same file is already in flight. Otherwise registers the path, runs the job,
    and deregisters it when the job finishes.
    """
    resolved = Path(path).resolve()
    if resolved.suffix.lower() != ".mkv":
        logger.warning("Refusing to queue non-MKV repair")
        return None
    key = str(resolved)
    with _repair_lock:
        if key in _in_flight_repairs:
            logger.info("Repair already in flight; ignoring duplicate")
            return None
        _in_flight_repairs.add(key)

    job = RepairJob(resolved)

    def _on_done(p: str, ok: bool, err: str) -> None:
        with _repair_lock:
            _in_flight_repairs.discard(key)
        on_done(p, ok, err)

    job.signals.done.connect(_on_done)
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
      * Repair work files (``*.repairing.mkv`` / ``*.broken-bak.mkv``) — they
        end in ``.mkv`` but are not recordings; re-muxing one would chain
        temps and never converge.
      * Files modified within ``min_age_seconds`` (probably still open).
      * Files smaller than ``min_size_bytes`` (uninteresting test artefacts).

    Synchronous, but each probe runs in ~50 ms so a folder of dozens of
    recordings is still well under a second.
    """
    skip = {p.resolve() for p in (skip_paths or ())}
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out: list[Path] = []
    now = time.time()
    for p in folder.iterdir():
        if not p.is_file() or p.suffix.lower() != ".mkv":
            continue
        if is_repair_temp(p):
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
        if not is_momento_owned(p):
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


def cleanup_stale_repair_temps(folder: Path, min_age_seconds: float = 120.0) -> int:
    """Delete orphaned repair work files left by a repair that never finished.

    A successful repair consumes its ``*.repairing.mkv`` (``os.replace`` moves
    it onto the original), so a temp that survives means a previous repair was
    killed mid-flight — leaving a full-size file tying up ~2× the recording's
    space. Only files untouched for ``min_age_seconds`` are removed: an active
    ``ffmpeg -c copy`` writes continuously, so a stale mtime is a reliable
    "no repair is using this" signal. Runs at startup, before the recovery
    scan queues fresh repairs, so it never races a live one.

    Returns the number of files deleted.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    now = time.time()
    removed = 0
    for p in folder.iterdir():
        try:
            if not p.is_file() or not is_repair_temp(p):
                continue
            st = p.stat()
        except OSError:
            continue
        if now - st.st_mtime < min_age_seconds:
            continue
        original = recording_path_for_repair_temp(p)
        if original is None or not is_momento_owned(original):
            continue
        try:
            p.unlink()
        except OSError as e:
            logger.warning("Could not remove stale repair temp: %s", e)
            continue
        removed += 1
        logger.info(
            "Removed stale repair temp (%.1f MiB)",
            st.st_size / (1024 ** 2),
        )
    return removed
