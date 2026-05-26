"""Stream-copy MP4 trim via the bundled ffmpeg binary.

Command structure (matches the M10 spec exactly):

    ffmpeg -ss <start> -to <end> -i <input> -c copy <output>

Notes:
  * ``-c copy`` skips re-encoding, so trim points snap to the nearest
    keyframe before/at the requested time. Documented as a UI limitation.
  * Runs as a background QObject worker so the UI doesn't freeze.
  * ffmpeg's stderr is captured to %APPDATA%/Momento/logs/trim_*.log.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from momento.util.ffmpeg_path import ffmpeg_exe
from momento.util.paths import logs_dir

logger = logging.getLogger(__name__)


def clips_dir_for(input_path: Path) -> Path:
    """Return the directory clips should be written to.

    Clips always live in ``<recordings_folder>/clips/``. When the user
    trims an existing clip (re-trim), the input is already in ``clips/``
    and the output stays alongside it.
    """
    parent = input_path.parent
    return parent if parent.name == "clips" else parent / "clips"


def next_clip_path(input_path: Path) -> Path:
    """Return ``clips/{stem}_clip_{n}.mp4`` for the smallest n not yet on disk."""
    stem = input_path.stem
    clips_dir = clips_dir_for(input_path)
    n = 1
    while True:
        candidate = clips_dir / f"{stem}_clip_{n}.mp4"
        if not candidate.exists():
            return candidate
        n += 1
        if n > 9999:
            raise RuntimeError("Ran out of clip-number slots")


class TrimWorker(QObject):
    """Runs one ffmpeg trim subprocess. Emits progress / done / failed.

    Instantiate, move to a QThread, connect ``thread.started`` to ``run``,
    then start the thread. The worker emits ``done(output_path)`` or
    ``failed(message)`` exactly once and then ``finished`` (for cleanup).
    """

    progress = pyqtSignal(float, float)  # current_seconds, total_seconds
    done = pyqtSignal(str)  # absolute output path
    failed = pyqtSignal(str)  # human-readable message
    finished = pyqtSignal()

    def __init__(
        self,
        input_path: Path,
        start: float,
        end: float,
        output_path: Path,
    ) -> None:
        super().__init__()
        self._input = input_path
        self._start = float(start)
        self._end = float(end)
        self._output = output_path
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self) -> None:
        try:
            self._run_inner()
        except Exception as e:
            logger.exception("Unexpected trim error")
            self.failed.emit(str(e))
        finally:
            self.finished.emit()

    # ----------------------------------------------------------- internals
    def _run_inner(self) -> None:
        if self._end <= self._start:
            self.failed.emit("End must be greater than start")
            return
        total = self._end - self._start
        ffmpeg = ffmpeg_exe()
        # The caller decides the output path, but we make sure the parent
        # exists — ``clips/`` may not yet be present in a brand-new folder.
        try:
            self._output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.failed.emit(f"Could not create output folder: {e}")
            return

        args = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel", "info",
            "-y",
            "-ss", f"{self._start:.3f}",
            "-to", f"{self._end:.3f}",
            "-i", str(self._input),
            # Carry over the MKV's container tags (notably MOMENTO_GAME so
            # the editor's game filter groups clips with their parent
            # recording even after rename). `-map_metadata 0` selects the
            # input's tags; `use_metadata_tags` tells the MP4 muxer to
            # actually write them — without it the MP4 muxer drops anything
            # that isn't a standard 4-char atom.
            "-map_metadata", "0",
            "-c", "copy",
            "-movflags", "+faststart+use_metadata_tags",
            str(self._output),
        ]

        log_path = _new_log_path(self._output)
        log_fh = open(log_path, "wb")
        log_fh.write(
            ("ffmpeg args: " + " ".join(_quote_for_log(a) for a in args) + "\n\n").encode("utf-8")
        )
        log_fh.flush()

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as e:
            log_fh.close()
            self.failed.emit(f"Could not start ffmpeg: {e}")
            return

        # Tail stderr line-by-line, mirror to the log file, parse progress.
        proc = self._proc
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                if self._cancelled:
                    break
                log_fh.write(line.encode("utf-8", errors="replace"))
                cur = _parse_time(line)
                if cur is not None and total > 0:
                    self.progress.emit(min(cur, total), total)
        finally:
            log_fh.flush()
            log_fh.close()
            rc = proc.wait()

        if self._cancelled:
            try:
                self._output.unlink(missing_ok=True)
            except OSError:
                pass
            self.failed.emit("Cancelled")
            return

        if rc != 0:
            self.failed.emit(f"ffmpeg exited with code {rc} — see {log_path}")
            return
        if not self._output.exists():
            self.failed.emit(f"Output file missing — see {log_path}")
            return
        size = self._output.stat().st_size
        # A valid MP4 needs at least the ftyp+moov boxes — call it 4 KB. Anything
        # smaller means the disk filled or ffmpeg gave up before writing audio.
        if size < 4096:
            try:
                self._output.unlink(missing_ok=True)
            except OSError:
                pass
            self.failed.emit(
                f"Output file is suspiciously small ({size} bytes) — disk may be "
                f"full or the input has no data in the selected range. See {log_path}"
            )
            return

        self.done.emit(str(self._output))


# ----------------------------------------------------------------- helpers
_TIME_RE = re.compile(r"time=(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")


def _parse_time(line: str) -> float | None:
    m = _TIME_RE.search(line)
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2))
    s = int(m.group(3))
    frac = m.group(4)
    seconds = h * 3600 + mn * 60 + s
    if frac:
        seconds += float("0." + frac)
    return float(seconds)


def _new_log_path(output_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return logs_dir() / f"trim_{output_path.stem}_{stamp}.log"


def _quote_for_log(arg: str) -> str:
    return f'"{arg}"' if " " in arg or "=" in arg else arg
