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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from momento.core import audio_devices
from momento.core.audio_loopback import LoopbackStreamer
from momento.core.encoder import InProcessEncoder, is_output_write_error
from momento.core import encoders
from momento.core.mic_capture import MicStreamer
from momento.core.video_capture import WindowVideoStreamer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingParams:
    output_path: Path  # final .mkv path the caller asked for
    hwnd: int
    mic_device: str
    audio_device: str
    mic_volume_pct: int = 100
    audio_volume_pct: int = 100
    framerate: int = 60
    audio_offset_ms: int = 0
    game_slug: str | None = None  # written as container metadata; see encoder.py
    # Capture-quality knobs threaded through from Config:
    target_resolution: str = "source"  # source | 1080p | 1440p | 4k
    quality_preset: str = "high"       # low | medium | high | custom
    custom_bitrate_kbps: int = 12_000


class RecordingFinalizeError(RuntimeError):
    """Raised by :meth:`Recorder.stop` when a recording did not finalise cleanly.

    The MKV is usually still on disk and recoverable — its clusters are intact
    and the repair path (``ffmpeg -genpts``) regenerates a seekable file — but
    the finalize (encoder flush / ``container.close()``) failed or a worker
    hung. Callers must NOT report the recording as a clean save; the session
    surfaces it as a warning and leaves the file for the repair path.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path.name}: {detail}")
        self.path = path
        self.detail = detail


class RecordingStartCancelled(RuntimeError):
    """Raised when stop() cancels a pipeline that is still starting."""


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
        self._start_monotonic: float | None = None
        self._lock = threading.Lock()
        self._is_running = False
        # True while start() is building the pipeline outside the lock —
        # claims the start so a concurrent start() can't interleave.
        self._starting = False
        self._stop_requested = False
        # Audio callbacks originate on capture threads and may race the final
        # pipeline publish. A generation prevents a late callback from an old
        # recording mutating a reused Recorder, while the failure flags ensure
        # an early callback wins over the local streamer reference at publish.
        self._audio_generation = 0
        self._mic_failed = False
        self._sys_failed = False
        self._encoder_failed: Exception | None = None
        self._video_degraded_warned = False
        self._active_video_codec: str | None = None
        # Set by the owner (SessionManager). Invoked when the captured game
        # window is destroyed mid-recording, so the session can finalise
        # promptly instead of waiting for the watcher to see process death.
        self.on_window_closed: Callable[[], None] | None = None
        # Set by the owner (SessionManager). Invoked with a leg name ("mic" /
        # "system audio") when a configured audio device dies MID-recording, so
        # the loss can be surfaced instead of silently producing a track-less
        # clip. (Start-time open failures are reported separately via the
        # mic_dropped/sys_dropped properties at publish.)
        self.on_audio_dropped: Callable[[str], None] | None = None
        self.on_encoder_failed: Callable[[Exception], None] | None = None
        self.on_video_capture_failed: Callable[[Exception], None] | None = None
        self.on_video_degraded: Callable[[float], None] | None = None

    # ------------------------------------------------------------------ API
    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_running

    @property
    def is_busy(self) -> bool:
        """True while a recording is running or still building its pipeline."""
        with self._lock:
            return self._is_running or self._starting

    @property
    def active_video_codec(self) -> str | None:
        with self._lock:
            return self._active_video_codec

    @property
    def mic_dropped(self) -> bool:
        """A mic device was configured but isn't being captured (failed to open
        or died) while a recording is live — used to warn the user."""
        with self._lock:
            return (
                self._is_running
                and self._params is not None
                and bool(self._params.mic_device)
                and (self._mic is None or self._mic_failed)
            )

    @property
    def sys_dropped(self) -> bool:
        """A system-audio device was configured but isn't being captured."""
        with self._lock:
            return (
                self._is_running
                and self._params is not None
                and bool(self._params.audio_device)
                and (self._loopback is None or self._sys_failed)
            )

    def _handle_audio_capture_failure(
        self,
        name: str,
        leg: str,
        generation: int,
        close_input: Callable[[], None],
        exc: Exception,
    ) -> None:
        """An audio capture stream died mid-recording (e.g. device unplugged).

        EOF the encoder input so the filter graph doesn't wait on it, null the
        streamer so mic_dropped/sys_dropped reflect reality, and notify the owner
        so the loss is surfaced (the recording keeps going without that track).
        """
        logger.warning(
            "Continuing recording without %s; capture stopped unexpectedly: %s",
            name,
            str(exc).strip() or repr(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            close_input()
        except Exception:
            logger.exception("Error closing %s encoder input after capture failure", name)
        with self._lock:
            if generation != self._audio_generation:
                logger.debug("Ignoring stale %s callback from an older recording", name)
                return
            if leg == "loopback":
                self._sys_failed = True
                self._loopback = None
            else:
                self._mic_failed = True
                self._mic = None
            # During start(), SessionManager has not published _current_game yet.
            # It performs one attributed _warn_if_audio_dropped() immediately
            # after publish, so only fire the mid-recording callback when the
            # Recorder itself is already live.
            notify_owner = self._is_running
        if not notify_owner:
            return
        cb = self.on_audio_dropped
        if cb is not None:
            try:
                cb(name)
            except Exception:
                logger.exception("on_audio_dropped callback raised")

    def _handle_encoder_failure(
        self,
        generation: int,
        exc: Exception,
        video_codec: str | None = None,
    ) -> None:
        """Surface a live encoder-worker failure without waiting for game exit."""
        with self._lock:
            if generation != self._audio_generation:
                logger.debug("Ignoring stale encoder failure from an older recording")
                return
            first_failure = self._encoder_failed is None
            if first_failure:
                self._encoder_failed = exc
            notify_owner = first_failure and self._is_running
        if first_failure and video_codec and not is_output_write_error(exc):
            encoders.disable_for_process(video_codec, exc)
        if not notify_owner:
            return
        callback = self.on_encoder_failed
        if callback is not None:
            try:
                callback(exc)
            except Exception:
                logger.exception("on_encoder_failed callback raised")

    def _handle_video_degraded(self, generation: int, drop_rate: float) -> None:
        """Surface sustained frame loss once per recording without stopping it."""
        with self._lock:
            if generation != self._audio_generation:
                return
            notify_owner = self._is_running and not self._video_degraded_warned
            if notify_owner:
                self._video_degraded_warned = True
        if not notify_owner:
            return
        callback = self.on_video_degraded
        if callback is not None:
            try:
                callback(float(drop_rate))
            except Exception:
                logger.exception("on_video_degraded callback raised")

    def _handle_video_capture_failure(self, generation: int, exc: Exception) -> None:
        """Surface an unexpected WGC stop without labelling a clean MKV corrupt."""
        with self._lock:
            if generation != self._audio_generation:
                logger.debug("Ignoring stale video-capture failure from an older recording")
                return
            first_failure = self._encoder_failed is None
            if first_failure:
                # Reuse the startup cancellation flag: if WGC dies before the
                # pipeline is published, start() must not report success.
                self._encoder_failed = exc
            notify_owner = first_failure and self._is_running
        if not notify_owner:
            return
        callback = self.on_video_capture_failed
        if callback is not None:
            try:
                callback(exc)
            except Exception:
                logger.exception("on_video_capture_failed callback raised")

    def current_position(self) -> float | None:
        """Seconds elapsed since the current recording started, or None if idle."""
        with self._lock:
            if self._start_monotonic is None or not self._is_running:
                return None
            return max(0.0, time.monotonic() - self._start_monotonic)

    def start(
        self,
        output_path: Path | str,
        hwnd: int,
        mic_device: str,
        audio_device: str,
        mic_volume_pct: int = 100,
        audio_volume_pct: int = 100,
        framerate: int = 60,
        audio_offset_ms: int = 0,
        game_slug: str | None = None,
        target_resolution: str = "source",
        quality_preset: str = "high",
        custom_bitrate_kbps: int = 12_000,
    ) -> None:
        """Start a new recording. Raises if one is already in flight.

        The pipeline build (WGC prepare, encoder open, audio device opens)
        runs OUTSIDE ``self._lock``: the GUI thread polls ``is_recording`` /
        ``current_position()`` through that lock (status panel timer, tray),
        and the build legitimately blocks for seconds — WGC's settling wait
        plus up to 5s per audio open-confirmation — so holding the lock
        throughout froze an open editor for the duration. ``_starting``
        claims the start under the lock so a concurrent start() still can't
        interleave; the built pipeline is published under the lock at the end.
        """
        with self._lock:
            if self._is_running or self._starting:
                raise RuntimeError("Recorder.start called while a recording is in progress")
            self._starting = True
            self._stop_requested = False
            self._audio_generation += 1
            generation = self._audio_generation
            self._mic_failed = False
            self._sys_failed = False
            self._encoder_failed = None
            self._video_degraded_warned = False
            self._active_video_codec = None

        try:
            params = RecordingParams(
                output_path=Path(output_path).resolve(),
                hwnd=int(hwnd),
                mic_device=mic_device,
                audio_device=audio_device,
                mic_volume_pct=int(mic_volume_pct),
                audio_volume_pct=int(audio_volume_pct),
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
            # 1. Bring up WGC and learn the captured frame size. WGC's
            #    measured size can differ from GetWindowRect (shadow regions
            #    on Win11), so we wait for an actual frame before building
            #    the encoder. The sender thread is started below, after the
            #    encoder is ready to accept frames.
            video = WindowVideoStreamer(hwnd=params.hwnd, framerate=params.framerate)
            video.on_window_closed = lambda: self._on_video_window_closed(generation)
            video.on_capture_failed = lambda exc: self._handle_video_capture_failure(
                generation, exc
            )
            try:
                w, h = video.prepare()
            except Exception:
                logger.exception("Failed to prepare window video capture for hwnd=%d", params.hwnd)
                raise
            if self._start_cancel_requested():
                try:
                    video.stop()
                except Exception:
                    logger.exception("Error stopping cancelled video capture")
                raise RecordingStartCancelled("recording start cancelled before encoder open")

            target_w, target_h = _resolve_target_dims(
                w, h, params.target_resolution,
            )
            # Probe the exact target dimensions, frame rate, and quality mode.
            # A tiny generic probe can pass while a driver rejects the real
            # 1440p/4K session; trying every backend here lets AMD, Intel, MF,
            # and the CPU floor recover before recording begins.
            try:
                # PortAudio shared mode can't resample, so each audio stream
                # opens at its device's native rate; tell the encoder that rate
                # so its filter graph aresamples to 48 kHz. Pinning 48 kHz would
                # silently drop audio from any 44.1 kHz mic / headset / DAC.
                mic_rate = (
                    audio_devices.native_sample_rate(params.mic_device, loopback=False)
                    if params.mic_device else None
                ) or audio_devices.DEFAULT_SAMPLE_RATE
                sys_rate = (
                    audio_devices.native_sample_rate(params.audio_device, loopback=True)
                    if params.audio_device else None
                ) or audio_devices.DEFAULT_SAMPLE_RATE
                logger.info("Audio capture rates: mic=%d Hz system=%d Hz", mic_rate, sys_rate)
                startup_frame = video.startup_frame()
                excluded_codecs: set[str] = set()
                last_encoder_error: Exception | None = None
                while True:
                    try:
                        video_codec = encoders.pick_encoder_for_recording(
                            width=target_w,
                            height=target_h,
                            framerate=params.framerate,
                            preset=params.quality_preset,
                            custom_bitrate_kbps=params.custom_bitrate_kbps,
                            excluded=excluded_codecs,
                        )
                    except RuntimeError as selection_error:
                        if last_encoder_error is not None:
                            raise RuntimeError(
                                "Every compatible H.264 encoder failed to open "
                                "the production recording pipeline."
                            ) from last_encoder_error
                        raise selection_error

                    video_options = encoders.quality_options_for(
                        video_codec,
                        params.quality_preset,
                        params.custom_bitrate_kbps,
                    )
                    encoder_pix_fmt = encoders.preferred_pix_fmt_for(video_codec)
                    logger.info(
                        "Selected video encoder candidate: %s (preset=%s, pix_fmt=%s)",
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
                        mic_sample_rate=mic_rate,
                        sys_sample_rate=sys_rate,
                        audio_offset_seconds=params.audio_offset_ms / 1000.0,
                        game_slug=params.game_slug,
                        target_width=target_w,
                        target_height=target_h,
                        video_codec=video_codec,
                        video_options=video_options,
                        encoder_pix_fmt=encoder_pix_fmt,
                    )
                    encoder.on_fatal_error = (
                        lambda exc, codec=video_codec, owner=encoder: self._handle_encoder_failure(
                            generation,
                            exc,
                            codec if owner.fatal_component == "video" else None,
                        )
                    )
                    encoder.on_video_degraded = (
                        lambda rate: self._handle_video_degraded(generation, rate)
                    )
                    try:
                        encoder.start(startup_video_frame=startup_frame)
                    except Exception as encoder_error:
                        if not _should_retry_encoder_candidate(encoder_error):
                            logger.error(
                                "Recording output failed while opening the production pipeline: %s",
                                str(encoder_error).strip() or repr(encoder_error),
                            )
                            raise
                        excluded_codecs.add(video_codec)
                        last_encoder_error = encoder_error
                        logger.warning(
                            "Video encoder candidate failed in production pipeline: %s (%s)",
                            video_codec,
                            str(encoder_error).strip() or repr(encoder_error),
                            exc_info=True,
                        )
                        try:
                            mkv_path.unlink(missing_ok=True)
                        except OSError:
                            logger.exception("Could not remove failed encoder attempt")
                        continue
                    break
            except Exception:
                try:
                    video.stop()
                except Exception:
                    logger.exception("Error stopping video capture after encoder start failure")
                logger.exception("Failed to pick encoder or start it")
                raise
            if self._start_cancel_requested():
                _stop_partial_pipeline(mkv_path, video=video, encoder=encoder)
                raise RecordingStartCancelled("recording start cancelled before capture began")

            loopback: LoopbackStreamer | None = None
            if params.audio_device:
                loopback = LoopbackStreamer(device_id=params.audio_device, sample_rate=sys_rate)
                loopback.on_capture_failed = (
                    lambda exc: self._handle_audio_capture_failure(
                        "system audio",
                        "loopback",
                        generation,
                        encoder.close_sys_audio,
                        exc,
                    )
                )
                try:
                    loopback.start(encoder.submit_sys_audio)
                except Exception as e:
                    logger.warning(
                        "Continuing recording without system audio; loopback failed to start: %s",
                        e,
                        exc_info=True,
                    )
                    encoder.close_sys_audio()
                    loopback = None
            else:
                logger.warning("Continuing recording without system audio; no device configured")
                encoder.close_sys_audio()
            if self._start_cancel_requested():
                _stop_partial_pipeline(
                    mkv_path, video=video, loopback=loopback, encoder=encoder
                )
                raise RecordingStartCancelled("recording start cancelled before mic opened")

            mic: MicStreamer | None = None
            if params.mic_device:
                mic = MicStreamer(device_id_or_name=params.mic_device, sample_rate=mic_rate)
                mic.on_capture_failed = (
                    lambda exc: self._handle_audio_capture_failure(
                        "mic", "mic", generation, encoder.close_mic_audio, exc
                    )
                )
                try:
                    mic.start(encoder.submit_mic_audio)
                except Exception as e:
                    logger.warning(
                        "Continuing recording without mic audio; mic failed to start: %s",
                        e,
                        exc_info=True,
                    )
                    encoder.close_mic_audio()
                    mic = None
            else:
                logger.warning("Continuing recording without mic audio; no device configured")
                encoder.close_mic_audio()
            if self._start_cancel_requested():
                _stop_partial_pipeline(
                    mkv_path, video=video, loopback=loopback, mic=mic, encoder=encoder
                )
                raise RecordingStartCancelled("recording start cancelled before publish")

            # Start the video timeline only after configured audio legs have
            # either opened or been explicitly closed. Starting video first
            # produced a visible silent lead-in while WASAPI devices opened.
            try:
                video.start_sending(
                    encoder.submit_video,
                    pts_clock=encoder.current_pts_seconds,
                )
            except Exception:
                _stop_partial_pipeline(
                    mkv_path, video=video, loopback=loopback, mic=mic, encoder=encoder
                )
                logger.exception("Failed to start video frame delivery")
                raise
        except Exception:
            with self._lock:
                self._starting = False
                self._stop_requested = False
            raise

        cancelled = False
        encoder_failure: Exception | None = None
        with self._lock:
            encoder_failure = self._encoder_failed
            if self._stop_requested or encoder_failure is not None:
                cancelled = True
                self._starting = False
                self._stop_requested = False
            else:
                self._params = params
                self._encoder = encoder
                self._active_video_codec = video_codec
                self._video = video
                self._loopback = None if self._sys_failed else loopback
                self._mic = None if self._mic_failed else mic
                self._mkv_path = mkv_path
                self._start_monotonic = time.monotonic()
                self._is_running = True
                self._starting = False
        if cancelled:
            _stop_partial_pipeline(
                mkv_path, video=video, loopback=loopback, mic=mic, encoder=encoder
            )
            if encoder_failure is not None:
                raise RuntimeError(
                    f"Encoder failed during recording startup: {encoder_failure}"
                ) from encoder_failure
            raise RecordingStartCancelled("recording start cancelled at publish")
        logger.info(
            "Recording started: hwnd=%d size=%dx%d framerate=%d",
            params.hwnd, w, h, params.framerate,
        )

    def _on_video_window_closed(self, generation: int) -> None:
        """WGC told us the captured window was destroyed. Forward to the owner
        for a live recording. During startup it records a terminal failure so
        the pipeline cannot be published after its capture source has died.
        Runs on the WGC thread; the owner hands live finalisation to another
        thread."""
        with self._lock:
            if generation != self._audio_generation:
                return
            if self._starting and not self._is_running:
                if self._encoder_failed is None:
                    self._encoder_failed = RuntimeError(
                        "Captured game window closed during recording startup"
                    )
                return
            if not self._is_running:
                return
        cb = self.on_window_closed
        if cb is not None:
            try:
                cb()
            except Exception:
                logger.exception("Recorder.on_window_closed callback raised")

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
            if self._starting and not self._is_running:
                self._stop_requested = True
                return None
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
            self._active_video_codec = None
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
        fatal: Exception | None = None
        try:
            stats = encoder.stop()
            logger.info("Encoder done: %s", stats.summary())
            fatal = encoder.fatal_error
        except Exception as e:
            logger.exception("Error stopping encoder")
            fatal = e

        # A finalize failure (flush/close error, or a hung worker the encoder
        # refused to race) must NOT be reported as a clean save. The MKV's
        # clusters are on disk and recoverable, so we keep the file and raise —
        # the session warns the user and the repair path / startup recovery
        # regenerate a seekable file. This is what stops a disk-full or libav
        # close failure from silently producing a "saved" but corrupt recording.
        if fatal is not None:
            raise RecordingFinalizeError(
                mkv_path, str(fatal) or "encoder did not finalise cleanly"
            )

        if not mkv_path.exists():
            logger.error("MKV file missing after encoder stop")
            raise RecordingFinalizeError(mkv_path, "output file missing after finalize")

        logger.info("Recording finalised")
        return mkv_path

    def cancel_start(self) -> bool:
        """Cancel only an unpublished pipeline build, never a live recording."""
        with self._lock:
            if not self._starting or self._is_running:
                return False
            self._stop_requested = True
            return True

    def _start_cancel_requested(self) -> bool:
        with self._lock:
            return self._starting and self._stop_requested


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


def _stop_partial_pipeline(
    mkv_path: Path,
    *,
    video=None,
    loopback=None,
    mic=None,
    encoder: InProcessEncoder | None = None,
) -> None:
    """Best-effort teardown for a start() that failed before publish."""
    for component, name in (
        (mic, "mic"),
        (loopback, "system audio"),
        (video, "video"),
    ):
        if component is None:
            continue
        try:
            component.stop()
        except Exception:
            logger.exception("Error stopping partial-start %s capture", name)
    if encoder is not None:
        try:
            encoder.stop()
        except Exception:
            logger.exception("Error stopping partial-start encoder")
    _discard_failed_output(mkv_path)


def _discard_failed_output(mkv_path: Path) -> None:
    """Best-effort removal of the MKV a failed start leaves behind.

    encoder.start() creates the file; if an audio stream then fails to open,
    the user gets a "couldn't record" toast — leaving the finalised stub (or
    a few seconds of silent video) behind would surface a junk 0:00 card in
    the editor's library on every failed start.
    """
    try:
        mkv_path.unlink(missing_ok=True)
        logger.info("Removed output of failed start")
    except OSError:
        logger.warning("Could not remove failed-start output", exc_info=True)


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


def _should_retry_encoder_candidate(exc: BaseException) -> bool:
    """Backend fallback cannot heal a full, removed, or read-only output drive."""
    return not is_output_write_error(exc)
