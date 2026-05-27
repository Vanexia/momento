"""In-process window recorder.

This module owns the live recording pipeline. Architecture:

    WGC capture (BGRA frames) --\\
    WASAPI loopback (system)   --+-> InProcessEncoder --> .mkv (final)
    WASAPI mic                 --/

Recordings land as **MKV** and stay that way — matching OBS's default. MKV
is cluster-based and self-recoverable, so a hard process kill mid-record
still leaves a playable file. Trim export produces MP4 with ``+faststart``
(via the bundled ffmpeg.exe stream-copying), so the share-out artefact is
the universally-compatible container; the local library stays MKV.

Compared to the old subprocess+TCP path, every failure surface that bit us
in the FFXIV repro is gone:
  * No localhost TCP — capture submits frames directly into a bounded
    queue. Slow encoder = dropped frames at the queue boundary, never
    cascading sendall stalls.
  * No subprocess lifecycle — encoder lives in our Python process. Clean
    shutdown is a synchronous flush(), no ``q\\n`` race, no ``terminate``
    fallback.
  * No moov-atom-at-end failure mode — MKV is incrementally finalised, and
    we never remux on the recording path. The bundled ffmpeg is only
    invoked offline for trim export.
  * Synchronous error feedback — a libav exception surfaces immediately
    in the worker thread, not 13 seconds later via ffmpeg stderr parsing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from momento.core.audio_loopback import LoopbackStreamer
from momento.core.encoder import InProcessEncoder
from momento.core import encoders
from momento.core.mic_capture import MicStreamer
from momento.core.video_capture import WindowVideoStreamer
from momento.util.paths import logs_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingParams:
    output_path: Path  # final .mkv path the caller asked for
    hwnd: int
    mic_device: str
    audio_device: str
    mic_volume_pct: int = 100
    audio_volume_pct: int = 100
    resolution: tuple[int, int] | None = None
    framerate: int = 60
    audio_offset_ms: int = 0
    game_slug: str | None = None  # written as container metadata; see encoder.py
    # Capture-quality knobs threaded through from Config:
    target_resolution: str = "source"  # source | 1080p | 1440p | 4k
    quality_preset: str = "high"       # low | medium | high | custom
    custom_bitrate_kbps: int = 12_000


class Recorder:
    """Owns one in-process encoder + capture threads for a single recording.

    A Recorder instance can be reused across many recordings — each
    :meth:`start` builds a fresh pipeline. :meth:`stop` flushes the encoder
    and closes the MKV; no remux happens on the live path.
    """

    def __init__(self) -> None:
        self._params: RecordingParams | None = None
        self._encoder: InProcessEncoder | None = None
        self._video: WindowVideoStreamer | None = None
        self._loopback: LoopbackStreamer | None = None
        self._mic: MicStreamer | None = None
        self._mkv_path: Path | None = None
        self._log_path: Path | None = None
        self._start_monotonic: float | None = None
        self._lock = threading.Lock()
        self._is_running = False

    # ------------------------------------------------------------------ API
    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_running

    def current_position(self) -> float | None:
        """Seconds elapsed since the current recording started, or None if idle."""
        with self._lock:
            if self._start_monotonic is None or not self._is_running:
                return None
            return max(0.0, time.monotonic() - self._start_monotonic)

    def current_output_path(self) -> Path | None:
        """The MKV path being written to. Exists on disk during recording
        (Matroska is incrementally finalised) and is the canonical artefact
        when stop() returns."""
        with self._lock:
            return self._mkv_path

    def start(
        self,
        output_path: Path | str,
        hwnd: int,
        mic_device: str,
        audio_device: str,
        mic_volume_pct: int = 100,
        audio_volume_pct: int = 100,
        resolution: tuple[int, int] | None = None,
        framerate: int = 60,
        audio_offset_ms: int = 0,
        game_slug: str | None = None,
        target_resolution: str = "source",
        quality_preset: str = "high",
        custom_bitrate_kbps: int = 12_000,
    ) -> None:
        """Start a new recording. Raises if one is already in flight."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Recorder.start called while a recording is in progress")

            params = RecordingParams(
                output_path=Path(output_path).resolve(),
                hwnd=int(hwnd),
                mic_device=mic_device,
                audio_device=audio_device,
                mic_volume_pct=int(mic_volume_pct),
                audio_volume_pct=int(audio_volume_pct),
                resolution=tuple(map(int, resolution)) if resolution else None,
                framerate=int(framerate),
                audio_offset_ms=int(audio_offset_ms),
                game_slug=game_slug,
                target_resolution=target_resolution,
                quality_preset=quality_preset,
                custom_bitrate_kbps=int(custom_bitrate_kbps),
            )
            try:
                params.output_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(
                    f"Output folder {params.output_path.parent} is not writable: {e}"
                ) from e
            if not _is_writable(params.output_path.parent):
                raise RuntimeError(
                    f"Output folder {params.output_path.parent} exists but cannot be "
                    "written to (drive removed / read-only / permission denied)."
                )

            # The caller passes a .mkv path; SessionManager builds it that way
            # so the recordings list displays the file with its real extension
            # both during and after recording.
            mkv_path = params.output_path
            if mkv_path.suffix.lower() != ".mkv":
                mkv_path = mkv_path.with_suffix(".mkv")
            self._log_path = _new_recording_log_path(mkv_path)

            # 1. Bring up WGC and learn the captured frame size. WGC's
            #    measured size can differ from GetWindowRect (shadow regions
            #    on Win11), so we wait for an actual frame before building
            #    the encoder. The sender thread is started below, after the
            #    encoder is ready to accept frames.
            video = WindowVideoStreamer(hwnd=params.hwnd, framerate=params.framerate)
            try:
                w, h = video.prepare()
            except Exception:
                logger.exception("Failed to prepare window video capture for hwnd=%d", params.hwnd)
                raise

            target_w, target_h = _resolve_target_dims(
                w, h, params.target_resolution,
            )
            # Auto-detect the best H.264 encoder available on this machine.
            # NVENC first, then AMF, QSV, MF, finally libx264 as software
            # floor. The probe + cache lives in momento.core.encoders so
            # the picked backend is consistent across recordings in the
            # same process run. This is INSIDE the same try/except that
            # owns video.stop() — if pick_encoder() raises (no encoder at
            # all works on this machine, libav corrupt, etc.) the WGC
            # capture session built above is correctly torn down.
            try:
                video_codec = encoders.pick_encoder()
                video_options = encoders.quality_options_for(
                    video_codec,
                    params.quality_preset,
                    params.custom_bitrate_kbps,
                )
                encoder_pix_fmt = encoders.preferred_pix_fmt_for(video_codec)
                logger.info(
                    "Selected video encoder: %s (preset=%s, pix_fmt=%s)",
                    encoders.display_name_for(video_codec),
                    params.quality_preset,
                    encoder_pix_fmt,
                )
                encoder = InProcessEncoder(
                    output_path=mkv_path,
                    video_width=w,
                    video_height=h,
                    video_framerate=params.framerate,
                    video_pix_fmt="bgra",
                    mic_volume=max(0.0, params.mic_volume_pct / 100.0),
                    sys_volume=max(0.0, params.audio_volume_pct / 100.0),
                    audio_offset_seconds=params.audio_offset_ms / 1000.0,
                    game_slug=params.game_slug,
                    target_width=target_w,
                    target_height=target_h,
                    video_codec=video_codec,
                    video_options=video_options,
                    encoder_pix_fmt=encoder_pix_fmt,
                )
                encoder.start()
            except Exception:
                video.stop()
                logger.exception("Failed to pick encoder or start it")
                raise

            video.start_sending(encoder.submit_video)

            loopback = LoopbackStreamer(device_id=params.audio_device)
            try:
                loopback.start(encoder.submit_sys_audio)
            except Exception:
                video.stop()
                encoder.stop()
                logger.exception("Failed to start system audio loopback")
                raise

            mic = MicStreamer(device_id_or_name=params.mic_device)
            try:
                mic.start(encoder.submit_mic_audio)
            except Exception:
                loopback.stop()
                video.stop()
                encoder.stop()
                logger.exception("Failed to start mic capture")
                raise

            self._params = params
            self._encoder = encoder
            self._video = video
            self._loopback = loopback
            self._mic = mic
            self._mkv_path = mkv_path
            self._start_monotonic = time.monotonic()
            self._is_running = True
            logger.info(
                "Recording started: hwnd=%d size=%dx%d framerate=%d -> %s",
                params.hwnd, w, h, params.framerate, mkv_path,
            )

    def stop(self) -> Path | None:
        """Stop the current recording and finalise the MKV.

        Shutdown order:
          1. Stop capture threads (no new frames submitted).
          2. Stop encoder (drains queues, flushes encoders, closes MKV).

        Returns the finalised MKV path, or None if nothing was recording.
        Following OBS, we do not auto-remux to MP4 — MKV is the canonical
        on-disk format. Trim export (which is what the user shares out)
        emits MP4 instead.
        """
        with self._lock:
            if not self._is_running:
                return None
            params = self._params
            encoder = self._encoder
            video = self._video
            loopback = self._loopback
            mic = self._mic
            mkv_path = self._mkv_path
            self._is_running = False
            self._encoder = None
            self._video = None
            self._loopback = None
            self._mic = None
            self._start_monotonic = None

        if encoder is None or mkv_path is None or params is None:
            return None

        # 1. Capture threads stop first so nothing new submits to the encoder.
        for cap, name in ((video, "video"), (loopback, "system audio"), (mic, "mic")):
            if cap is None:
                continue
            try:
                cap.stop()
            except Exception:
                logger.exception("Error stopping %s capture", name)

        # 2. Encoder drains and finalises the MKV.
        try:
            stats = encoder.stop()
            logger.info("Encoder done: %s", stats.summary())
        except Exception:
            logger.exception("Error stopping encoder")
            stats = None

        if encoder.fatal_error is not None:
            logger.error("Encoder reported fatal error: %r", encoder.fatal_error)

        if not mkv_path.exists():
            logger.error("MKV file missing after encoder stop: %s", mkv_path)
            return None

        logger.info("Recording finalised: %s", mkv_path)
        return mkv_path


# --------------------------------------------------------------- helpers
_RESOLUTION_HEIGHTS: dict[str, int] = {
    "1080p": 1080,
    "1440p": 1440,
    "4k": 2160,
}

def _resolve_target_dims(
    source_w: int, source_h: int, preset: str
) -> tuple[int, int]:
    """Convert a target_resolution preset into concrete (w, h).

    ``source`` (and any unrecognised value) returns the source dimensions.
    Numeric presets downscale, preserving the source aspect ratio, and only
    when they're smaller than the source — Momento never upscales. Output
    dimensions are forced even for NVENC + yuv420p.
    """
    if preset == "source" or preset not in _RESOLUTION_HEIGHTS:
        return source_w, source_h
    target_h = _RESOLUTION_HEIGHTS[preset]
    if target_h >= source_h:
        return source_w, source_h
    target_w = int(round(source_w * target_h / source_h))
    target_h -= target_h & 1
    target_w -= target_w & 1
    return target_w, target_h


def _is_writable(folder: Path) -> bool:
    """Probe whether ``folder`` accepts writes (drive present, ACL allows)."""
    import os
    import uuid

    if not folder.is_dir():
        return False
    probe = folder / f".momento_write_probe_{uuid.uuid4().hex}.tmp"
    try:
        with open(probe, "wb") as fh:
            fh.write(b"ok")
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    return True


def _new_recording_log_path(output_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return logs_dir() / f"recorder_{output_path.stem}_{stamp}.log"
