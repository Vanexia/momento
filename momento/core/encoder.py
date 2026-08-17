"""In-process video + audio encoder backed by PyAV (libav).

Replaces the old subprocess-ffmpeg + TCP-streamer architecture. Callers push
captured frames directly into this object; a worker thread pulls from bounded
queues and feeds PyAV's encoders, then muxes packets to an MKV file.

Recording invariant: every recording is written to ``*.mkv``. MKV is
cluster-based and self-recoverable, so even a hard process kill leaves a
playable file. The Recorder class is responsible for the offline MKV->MP4
remux step after :meth:`stop` returns.

Pipeline:

    capture threads --submit_video()--> [video queue] --encode--> mux
                    --submit_mic_audio()/sys_audio() --> [filter graph (amix)]
                                                         --encode--> mux

Backpressure policy:
    Submitting is non-blocking. Each input has a bounded queue. When the
    queue is full, the *oldest* item is dropped and a per-input drop counter
    increments. The capture thread never stalls, which is the entire point
    of the rewrite — the old TCP path back-pressured into capture and the
    pipeline collapsed.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import av.filter
import numpy as np

from momento.core.media_probe import MOMENTO_GAME_TAG

logger = logging.getLogger(__name__)


# Bounded queue sizes — small enough to keep latency low, big enough to absorb
# brief stalls without dropping. Tune later if needed.
_VIDEO_QUEUE_MAX = 8       # ~130 ms at 60 fps
_AUDIO_QUEUE_MAX = 32      # ~640 ms at 48k / 960-frame chunks
_DRAIN_TIMEOUT_S = 5.0
# Audio PTS is advanced by accumulated sample count, but if the capture thread
# falls behind and WASAPI drops input samples, that count lags the real
# (wallclock) time and audio creeps ahead of video over a long recording. When
# the count drifts more than this far behind wallclock we snap it forward to
# realign. Big enough to ignore normal jitter; small enough that a dropped patch
# is an inaudible gap, not creeping desync.
_AUDIO_RESYNC_THRESHOLD_S = 0.1
# If one audio leg stops delivering chunks for this long while the OTHER leg is
# still flowing, EOF the stalled leg. amix(duration=longest) otherwise emits
# NOTHING (it waits on the starved input's timeline) and buffers the flowing leg
# without bound — a fully silent track plus multi-GB RAM growth over a long
# recording. A healthy-but-quiet mic still delivers (silence) frames, so this
# only trips on a genuine capture stall, never on quiet audio.
_AUDIO_STALL_TIMEOUT_S = 10.0
# Treat loss as sustained only after five seconds at 60 fps, then warn at a
# rate high enough to represent visible degradation rather than a brief stall.
_VIDEO_DROP_WINDOW_FRAMES = 300
_VIDEO_DROP_WARN_RATE = 0.05
_VIDEO_DROP_WARN_INTERVAL_S = 60.0


@dataclass
class EncoderStats:
    video_frames_submitted: int = 0
    video_frames_encoded: int = 0
    video_frames_dropped: int = 0
    mic_chunks_submitted: int = 0
    mic_chunks_dropped: int = 0
    sys_chunks_submitted: int = 0
    sys_chunks_dropped: int = 0
    duration_s: float = 0.0
    output_path: Path | None = None

    @property
    def video_drop_rate(self) -> float:
        if self.video_frames_submitted <= 0:
            return 0.0
        return self.video_frames_dropped / self.video_frames_submitted

    @property
    def video_health_degraded(self) -> bool:
        return (
            self.video_frames_submitted >= _VIDEO_DROP_WINDOW_FRAMES
            and self.video_drop_rate >= _VIDEO_DROP_WARN_RATE
        )

    def summary(self) -> str:
        health = "DEGRADED" if self.video_health_degraded else "ok"
        return (
            f"video: {self.video_frames_encoded}/{self.video_frames_submitted} "
            f"encoded (drops={self.video_frames_dropped}, "
            f"loss={self.video_drop_rate:.1%}, health={health}); "
            f"mic drops={self.mic_chunks_dropped}/{self.mic_chunks_submitted}; "
            f"sys drops={self.sys_chunks_dropped}/{self.sys_chunks_submitted}; "
            f"duration={self.duration_s:.2f}s"
        )


@dataclass
class _VideoItem:
    array: np.ndarray
    pts: int


@dataclass
class _AudioItem:
    array: np.ndarray  # shape (frames, channels), float32
    pts_seconds: float


class InProcessEncoder:
    """Owns a libav output container, encoder threads, and bounded input queues.

    All submission methods are non-blocking and thread-safe.
    """

    def __init__(
        self,
        output_path: Path | str,
        *,
        video_width: int,
        video_height: int,
        video_framerate: int,
        video_pix_fmt: str = "bgra",
        mic_sample_rate: int = 48000,
        mic_channels: int = 2,
        sys_sample_rate: int = 48000,
        sys_channels: int = 2,
        mic_volume: float = 1.0,
        sys_volume: float = 1.0,
        audio_offset_seconds: float = 0.0,
        video_codec: str,
        video_options: dict[str, str],
        # Pixel format the chosen video_codec accepts. Phase 12 made this
        # explicit because QSV requires nv12 while everything else takes
        # yuv420p; the old NVENC-only path hardcoded yuv420p and the QSV
        # path silently broke. Recorder supplies this via
        # ``encoders.preferred_pix_fmt_for(video_codec)``.
        encoder_pix_fmt: str = "yuv420p",
        audio_codec: str = "aac",
        audio_bitrate: int = 192_000,
        game_slug: str | None = None,
        target_width: int | None = None,
        target_height: int | None = None,
    ) -> None:
        # MKV by default; the file extension carries the format implicitly.
        path = Path(output_path).resolve()
        if path.suffix.lower() != ".mkv":
            raise ValueError(
                f"output_path must end in .mkv (got {path.suffix!r}); "
                "the Recorder keeps MKV as the canonical recording format."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path

        # Matroska muxer options. Smaller clusters trade a bit of file-size
        # overhead for crash recoverability — a hard kill (TerminateProcess,
        # power loss, BSOD) mid-write only loses the current cluster, which
        # is now at most ~1 second instead of libav's default ~5s.
        #
        # We deliberately do NOT set ``reserve_index_space``: a multi-hour
        # recording accumulates more seek cues than any fixed reservation
        # holds (a ~5h capture overflowed 64 KiB), and on overflow libav
        # fails container.close() with EINVAL — leaving the ENTIRE file
        # unfinalised: no duration, no seek index, so it won't scrub and
        # ffprobe reports N/A. Letting the muxer append the cues at the end
        # on close has no size limit and seeks fine for local playback. It
        # costs nothing for crash recovery: a crashed (never-closed) file
        # never got its cues written either way, and the repair path
        # (ffmpeg -genpts) regenerates a seekable file exactly as before.
        self._mkv_options = {
            "cluster_time_limit": "1000",   # ms — cap cluster duration
            "cluster_size_limit": "2097152", # 2 MiB — cap cluster size
        }

        self._video_width = int(video_width)
        self._video_height = int(video_height)
        self._video_framerate = int(video_framerate)
        self._video_pix_fmt = video_pix_fmt
        # Stream-output dimensions (post-scale). Default = source size, in
        # which case the per-frame reformat step is a no-op and libav converts
        # BGRA into the chosen encoder's 4:2:0 pixel format at native size.
        self._target_width = int(target_width) if target_width else self._video_width
        self._target_height = int(target_height) if target_height else self._video_height
        # Snap to even values because the supported 4:2:0 formats require it.
        self._target_width -= self._target_width & 1
        self._target_height -= self._target_height & 1
        self._needs_scale = (
            self._target_width != self._video_width
            or self._target_height != self._video_height
        )

        self._mic_sample_rate = int(mic_sample_rate)
        self._mic_channels = int(mic_channels)
        self._sys_sample_rate = int(sys_sample_rate)
        self._sys_channels = int(sys_channels)
        self._mic_volume = float(mic_volume)
        self._sys_volume = float(sys_volume)
        # OBS-style audio sync offset. Negative shifts audio earlier in the
        # output (compensates for WASAPI loopback's inherent ~30-80ms latency
        # vs WGC's ~16-33ms compositor latency). Applied at the anchor step
        # for each audio stream — does not affect inter-chunk spacing.
        self._audio_offset_s = float(audio_offset_seconds)

        self._video_codec_name = video_codec
        self._video_options = dict(video_options)
        # Pix fmt the encoder accepts. yuv420p for almost everything;
        # nv12 for QSV. Set as the stream's on-disk format below.
        self._encoder_pix_fmt = str(encoder_pix_fmt)
        self._audio_codec_name = audio_codec
        self._audio_bitrate = int(audio_bitrate)
        # Identifier for the game this recording belongs to. Written as a
        # container-level Matroska tag so the editor can group recordings by
        # game even after the user renames the file — the filename prefix
        # is no longer the source of truth, this tag is.
        self._game_slug = (game_slug or "").strip() or None

        # libav objects, filled in by start()
        self._container: av.container.OutputContainer | None = None
        self._video_stream: av.video.stream.VideoStream | None = None
        self._audio_stream: av.audio.stream.AudioStream | None = None
        self._audio_graph: av.filter.Graph | None = None
        self._audio_buf_mic: av.filter.Filter | None = None
        self._audio_buf_sys: av.filter.Filter | None = None
        self._audio_sink: av.filter.Filter | None = None

        # The mixed audio stream that comes out of the filter graph. We pick
        # 48 kHz stereo as the output regardless of inputs; both inputs will
        # be resampled by the graph if needed.
        self._mixed_sample_rate = 48000
        self._mixed_layout = "stereo"

        # Bounded queues
        self._video_q: "queue.Queue[_VideoItem | None]" = queue.Queue(maxsize=_VIDEO_QUEUE_MAX)
        self._mic_q: "queue.Queue[_AudioItem | None]" = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)
        self._sys_q: "queue.Queue[_AudioItem | None]" = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)

        # Worker threads
        self._video_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None

        self._lock = threading.Lock()  # serialises container.mux() across threads
        self._stop_event = threading.Event()
        self._started = False
        self._fatal_error: Exception | None = None
        self._fatal_component: str | None = None
        self._fatal_lock = threading.Lock()
        self.on_fatal_error: Callable[[Exception], None] | None = None

        # Stats
        self._stats = EncoderStats(output_path=self._path)
        self._t0_monotonic: float | None = None
        self._video_drop_window: deque[bool] = deque(maxlen=_VIDEO_DROP_WINDOW_FRAMES)
        self._last_video_drop_warning_at = 0.0
        self._video_pts_lock = threading.Lock()
        self._last_video_pts: int | None = None

        # Audio PTS counters — the filter graph wants monotonically-increasing
        # PTS in each input's time_base. The counter is *anchored* on the
        # first submission per stream so audio PTS sits at the same wallclock
        # position as video PTS in the output container. Without this anchor,
        # audio always starts at file_pts=0 regardless of how late after the
        # encoder started it actually began arriving — manifesting as audio
        # ahead/behind video in playback depending on which side arrives
        # first.
        self._mic_pts_samples = 0
        self._sys_pts_samples = 0
        self._mic_anchored = False
        self._sys_anchored = False
        self._mic_input_closed = False
        self._sys_input_closed = False

        # Monotonic-PTS guard for the mixed audio output. amix can emit a frame
        # whose PTS steps BACKWARD (the two capture legs anchor to wallclock
        # independently and loopback can come online late, so amix re-mixes an
        # already-emitted region). Feeding a backward timestamp to the Matroska
        # muxer raises ArgumentError (EINVAL), which used to propagate out and
        # kill the whole audio worker — dropping ALL further audio and failing
        # finalize. We drop non-monotonic frames instead.
        self._last_audio_out_pts: int | None = None
        self._audio_pts_dropped = 0
        self._audio_mux_errors = 0
        self._audio_mux_error_logged = False
        # Wallclock of the last real chunk fed into each audio leg — drives the
        # stall watchdog (_check_audio_stall). Seeded to the start time in start().
        self._mic_last_data_wall = 0.0
        self._sys_last_data_wall = 0.0

    # ------------------------------------------------------------------ API
    @property
    def output_path(self) -> Path:
        return self._path

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop_event.is_set()

    def current_pts_seconds(self) -> float:
        """Return the current position on the encoder's shared A/V clock."""
        return self._wallclock_pts()

    @property
    def fatal_error(self) -> Exception | None:
        with self._fatal_lock:
            return self._fatal_error

    @property
    def fatal_component(self) -> str | None:
        with self._fatal_lock:
            return self._fatal_component

    def start(self, *, startup_video_frame: np.ndarray | None = None) -> None:
        """Open the output container and start the encoder threads.

        Raises on libav errors (encoder unavailable, file unwritable, ...).
        """
        if self._started:
            raise RuntimeError("InProcessEncoder already started")

        container = av.open(
            str(self._path), mode="w", format="matroska",
            options=self._mkv_options,
        )
        try:
            # Container-level metadata — libav writes these into Matroska's
            # \\Tags element. Must be set before the first packet is muxed.
            if self._game_slug:
                container.metadata[MOMENTO_GAME_TAG] = self._game_slug
            vs = container.add_stream(self._video_codec_name, rate=self._video_framerate)
            vs.width = self._target_width
            vs.height = self._target_height
            # Pix fmt is per-encoder: QSV needs nv12, everything else
            # takes yuv420p. yuv420p still gives the broadest player
            # compatibility once we remux to MP4; QSV's nv12 output is
            # also universally playable.
            vs.pix_fmt = self._encoder_pix_fmt
            vs.codec_context.options = self._video_options
            # Explicit keyframe interval: every 2 seconds. Without this
            # the various backends default to 250-frame GOPs (~4-8s at
            # typical capture rates), and the editor's seek/scrub for
            # bookmarks and trim handles has to decode an entire GOP
            # before landing — visibly sluggish on long recordings.
            vs.codec_context.gop_size = max(1, self._video_framerate * 2)
            # Millisecond-resolution timebase. PTS values come from wallclock
            # seconds * 1000, so reordering / drift is easy to reason about.
            vs.time_base = Fraction(1, 1000)
            # A codec can exist in libav yet fail when its hardware driver or
            # session initializes. Open this exact production stream now so
            # Recorder can try the next backend before publishing a session.
            vs.codec_context.open()

            ass = container.add_stream(self._audio_codec_name, rate=self._mixed_sample_rate)
            ass.bit_rate = self._audio_bitrate
            # AAC always wants fltp internally; PyAV handles the conversion.
            self._video_stream = vs
            self._audio_stream = ass
            self._container = container

            self._build_audio_graph()
            if startup_video_frame is not None:
                self._stats.video_frames_submitted += 1
                self._encode_one_video(_VideoItem(startup_video_frame, 0), vs)
                with self._video_pts_lock:
                    self._last_video_pts = 0
        except Exception:
            try:
                container.close()
            except Exception:
                pass
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            self._container = None
            raise

        self._t0_monotonic = time.monotonic()
        if startup_video_frame is None:
            with self._video_pts_lock:
                self._last_video_pts = None
        # Seed the stall watchdog's per-leg clocks to now, so a leg that never
        # comes online (vs. one that stalls mid-recording) is judged from start.
        self._mic_last_data_wall = self._t0_monotonic
        self._sys_last_data_wall = self._t0_monotonic
        self._stop_event.clear()

        self._video_thread = threading.Thread(
            target=self._video_worker, name="EncoderVideo", daemon=True
        )
        self._audio_thread = threading.Thread(
            target=self._audio_worker, name="EncoderAudio", daemon=True
        )
        self._video_thread.start()
        self._audio_thread.start()
        self._started = True
        logger.info(
            "InProcessEncoder started: %s %dx%d@%d %s -> %s",
            self._video_codec_name, self._video_width, self._video_height,
            self._video_framerate, self._video_options.get("preset", "?"),
            self._path,
        )

    def submit_video(self, frame: np.ndarray, pts_seconds: float | None = None) -> bool:
        """Submit a captured video frame.

        ``frame`` must match (height, width) of the configured stream. Format
        is whatever was passed to the constructor as ``video_pix_fmt``
        (default ``"bgra"``, shape (H, W, 4)).

        Returns True if queued, False if the queue was full (and the oldest
        item was dropped to make room).
        """
        if not self._started or self._stop_event.is_set() or self._fatal_error is not None:
            return False
        pts_seconds = pts_seconds if pts_seconds is not None else self._wallclock_pts()
        pts = max(0, int(pts_seconds * self._video_framerate + 0.5))
        with self._video_pts_lock:
            if self._last_video_pts is not None and pts <= self._last_video_pts:
                self._inc_submitted("video")
                self._inc_dropped("video")
                accepted = False
            else:
                self._last_video_pts = pts
                item = _VideoItem(array=frame, pts=pts)
                accepted = self._push_drop_oldest(self._video_q, item, "video")
        self._video_drop_window.append(not accepted)
        self._maybe_warn_video_drops()
        return accepted

    def submit_mic_audio(self, samples: np.ndarray, pts_seconds: float | None = None) -> bool:
        """Submit a mic audio chunk. ``samples`` is shape (frames, channels), float32."""
        if (
            not self._started
            or self._stop_event.is_set()
            or self._fatal_error is not None
            or self._mic_input_closed
        ):
            return False
        pts = pts_seconds if pts_seconds is not None else self._wallclock_pts()
        item = _AudioItem(array=samples, pts_seconds=pts)
        return self._push_drop_oldest(self._mic_q, item, "mic")

    def submit_sys_audio(self, samples: np.ndarray, pts_seconds: float | None = None) -> bool:
        """Submit a system-audio chunk. Same shape conventions as mic."""
        if (
            not self._started
            or self._stop_event.is_set()
            or self._fatal_error is not None
            or self._sys_input_closed
        ):
            return False
        pts = pts_seconds if pts_seconds is not None else self._wallclock_pts()
        item = _AudioItem(array=samples, pts_seconds=pts)
        return self._push_drop_oldest(self._sys_q, item, "sys")

    def close_mic_audio(self) -> None:
        """Mark the mic input as permanently unavailable for this recording."""
        self._queue_audio_eof(self._mic_q)

    def close_sys_audio(self) -> None:
        """Mark the system-audio input as permanently unavailable for this recording."""
        self._queue_audio_eof(self._sys_q)

    def stop(self) -> EncoderStats:
        """Flush queues, close encoders, finalize the MKV file.

        Returns an EncoderStats snapshot. Safe to call multiple times.

        Finalisation (encoder flush + ``container.close()``) only runs once the
        worker threads have *actually exited*. If a worker is still inside libav
        after the drain timeout (a hung hardware encoder), flushing or closing
        now would run CONCURRENTLY with that worker on the same stream/container
        — and libav encoders are not safe for concurrent use, so it would
        corrupt finalisation. In that case we leave the MKV un-finalised (its
        clusters are on disk and the repair path regenerates a seekable file)
        and record a fatal error, so the recording is NOT reported as a clean
        save. Any flush/close exception is likewise recorded as fatal rather
        than swallowed, so a disk-full / libav failure can't masquerade as a
        finalised recording.
        """
        if not self._started:
            return self._stats

        # Signal workers to drain and exit. Sentinel = None.
        self._stop_event.set()
        self._queue_eof(self._video_q)
        self._queue_audio_eof(self._mic_q)
        self._queue_audio_eof(self._sys_q)

        alive_threads: list[threading.Thread] = []
        for t in (self._video_thread, self._audio_thread):
            if t is not None:
                t.join(timeout=_DRAIN_TIMEOUT_S)
                if t.is_alive():
                    alive_threads.append(t)

        if alive_threads:
            # A worker is still inside libav encode/mux after the drain
            # timeout. Touching the stream/container now (flush or close) would
            # race it and corrupt the file. Do NOT finalise — leave the MKV
            # un-finalised (recoverable via repair) and mark fatal so the
            # caller does not report a clean save. No lock is taken: the hung
            # worker may be holding it inside mux(), and we must not deadlock.
            alive = [t.name or "worker" for t in alive_threads]
            msg = (
                f"encoder worker(s) {alive} did not drain within "
                f"{_DRAIN_TIMEOUT_S:.0f}s; leaving the MKV un-finalised "
                "(recoverable via repair) instead of racing them"
            )
            logger.error(msg)
            if self._fatal_error is None:
                self._fatal_error = RuntimeError(msg)
            self._started = False
            if self._t0_monotonic is not None:
                self._stats.duration_s = time.monotonic() - self._t0_monotonic
            self._schedule_late_container_close(alive_threads)
            return self._stats

        # Workers have exited — finalise with no concurrent libav use. Each
        # failure is recorded as fatal (first one wins) rather than swallowed.
        with self._lock:
            if self._container is not None:
                try:
                    if self._video_stream is not None:
                        for pkt in self._video_stream.encode(None):
                            self._container.mux(pkt)
                except Exception as e:
                    logger.exception("Error flushing video encoder")
                    if self._fatal_error is None:
                        self._fatal_error = e
                try:
                    if self._audio_stream is not None:
                        for pkt in self._audio_stream.encode(None):
                            self._container.mux(pkt)
                except Exception as e:
                    logger.exception("Error flushing audio encoder")
                    if self._fatal_error is None:
                        self._fatal_error = e
                try:
                    self._container.close()
                except Exception as e:
                    logger.exception("Error closing output container")
                    if self._fatal_error is None:
                        self._fatal_error = e
                self._container = None

        self._started = False
        if self._t0_monotonic is not None:
            self._stats.duration_s = time.monotonic() - self._t0_monotonic
        if self._stats.video_health_degraded:
            logger.warning(
                "Final video health DEGRADED: %d/%d frames dropped (%.1f%%) for %s",
                self._stats.video_frames_dropped,
                self._stats.video_frames_submitted,
                self._stats.video_drop_rate * 100.0,
                self._path,
            )
        logger.info("InProcessEncoder stopped: %s", self._stats.summary())
        return self._stats

    def _schedule_late_container_close(self, threads: list[threading.Thread]) -> None:
        """Release the container handle if timed-out workers later unwind.

        stop() must return dirty immediately after a drain timeout, but a
        worker can still recover a moment later. Waiting in a daemon cleanup
        thread lets us close the Matroska handle once no worker can touch libav
        anymore, without blocking shutdown or reporting the recording as clean.
        """
        def _late_close() -> None:
            for thread in threads:
                thread.join()
            with self._lock:
                container = self._container
                if container is None:
                    return
                try:
                    container.close()
                    logger.info("Closed timed-out encoder container after workers exited: %s", self._path)
                except Exception:
                    logger.exception("Late close of timed-out encoder container failed")
                finally:
                    self._container = None
                    self._video_stream = None
                    self._audio_stream = None
                    self._audio_graph = None
                    self._audio_buf_mic = None
                    self._audio_buf_sys = None
                    self._audio_sink = None

        threading.Thread(
            target=_late_close,
            name="EncoderLateClose",
            daemon=True,
        ).start()

    # ---------------------------------------------------------- internals
    def _wallclock_pts(self) -> float:
        if self._t0_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._t0_monotonic)

    def _push_drop_oldest(
        self, q: "queue.Queue[_VideoItem | _AudioItem | None]", item, kind: str
    ) -> bool:
        try:
            q.put_nowait(item)
            self._inc_submitted(kind)
            return True
        except queue.Full:
            try:
                q.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
                self._inc_submitted(kind)
                self._inc_dropped(kind)
                return False
            except queue.Full:
                self._inc_dropped(kind)
                return False

    def _queue_eof(self, q: "queue.Queue[_VideoItem | _AudioItem | None]") -> None:
        try:
            q.put_nowait(None)
        except queue.Full:
            # Force-drain one item to make room for the sentinel.
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def _queue_audio_eof(self, q: "queue.Queue[_AudioItem | None]") -> None:
        if not self._started:
            return
        self._queue_eof(q)

    def _record_worker_failure(self, error: Exception, *, component: str) -> None:
        """Close every input and report the first fatal worker exception."""
        with self._fatal_lock:
            first_failure = self._fatal_error is None
            if first_failure:
                self._fatal_error = error
                self._fatal_component = component
        self._stop_event.set()
        self._queue_eof(self._video_q)
        self._queue_eof(self._mic_q)
        self._queue_eof(self._sys_q)
        if not first_failure:
            return
        callback = self.on_fatal_error
        if callback is not None:
            try:
                callback(error)
            except Exception:
                logger.exception("Encoder fatal-error callback raised")

    def _maybe_warn_video_drops(self) -> None:
        if len(self._video_drop_window) < _VIDEO_DROP_WINDOW_FRAMES:
            return
        rolling_rate = sum(self._video_drop_window) / len(self._video_drop_window)
        if rolling_rate < _VIDEO_DROP_WARN_RATE:
            return
        now = time.monotonic()
        if now - self._last_video_drop_warning_at < _VIDEO_DROP_WARN_INTERVAL_S:
            return
        self._last_video_drop_warning_at = now
        logger.warning(
            "Video encoding degraded: rolling loss %.1f%% over %d frames; "
            "cumulative loss %.1f%% (%d/%d); queue=%d/%d; codec=%s; output=%s",
            rolling_rate * 100.0,
            len(self._video_drop_window),
            self._stats.video_drop_rate * 100.0,
            self._stats.video_frames_dropped,
            self._stats.video_frames_submitted,
            self._video_q.qsize(),
            _VIDEO_QUEUE_MAX,
            self._video_codec_name,
            self._path,
        )

    def _inc_submitted(self, kind: str) -> None:
        if kind == "video":
            self._stats.video_frames_submitted += 1
        elif kind == "mic":
            self._stats.mic_chunks_submitted += 1
        elif kind == "sys":
            self._stats.sys_chunks_submitted += 1

    def _inc_dropped(self, kind: str) -> None:
        if kind == "video":
            self._stats.video_frames_dropped += 1
        elif kind == "mic":
            self._stats.mic_chunks_dropped += 1
        elif kind == "sys":
            self._stats.sys_chunks_dropped += 1

    def _build_audio_graph(self) -> None:
        """Build the libav filter graph that mixes mic + system audio."""
        g = av.filter.Graph()
        mic = g.add_abuffer(
            sample_rate=self._mic_sample_rate,
            format="flt",
            layout=_channels_to_layout(self._mic_channels),
            time_base=Fraction(1, self._mic_sample_rate),
        )
        sys_ = g.add_abuffer(
            sample_rate=self._sys_sample_rate,
            format="flt",
            layout=_channels_to_layout(self._sys_channels),
            time_base=Fraction(1, self._sys_sample_rate),
        )
        vol_mic = g.add("volume", f"volume={self._mic_volume:.4f}")
        vol_sys = g.add("volume", f"volume={self._sys_volume:.4f}")
        # amix wants matching sample rates / layouts on its inputs; insert
        # aresamples to coerce both to the mixed output format.
        ar_mic = g.add(
            "aresample",
            f"sample_rate={self._mixed_sample_rate}",
        )
        ar_sys = g.add(
            "aresample",
            f"sample_rate={self._mixed_sample_rate}",
        )
        mix = g.add(
            "amix",
            "inputs=2:normalize=0:duration=longest:dropout_transition=0",
        )
        # Force the post-amix audio into the layout the AAC encoder will
        # consume. amix sometimes emits "mono" if both inputs collapse.
        fmt = g.add(
            "aformat",
            f"sample_rates={self._mixed_sample_rate}:sample_fmts=fltp:channel_layouts={self._mixed_layout}",
        )
        sink = g.add("abuffersink")

        mic.link_to(vol_mic)
        vol_mic.link_to(ar_mic)
        ar_mic.link_to(mix, 0, 0)

        sys_.link_to(vol_sys)
        vol_sys.link_to(ar_sys)
        ar_sys.link_to(mix, 0, 1)

        mix.link_to(fmt)
        fmt.link_to(sink)
        g.configure()

        self._audio_graph = g
        self._audio_buf_mic = mic
        self._audio_buf_sys = sys_
        self._audio_sink = sink

    # ------------------------------------------------------------- workers
    def _video_worker(self) -> None:
        assert self._video_stream is not None
        assert self._container is not None
        stream = self._video_stream
        try:
            while True:
                item = self._video_q.get()
                if item is None:
                    break
                self._encode_one_video(item, stream)
        except Exception as e:
            self._record_worker_failure(e, component="video")
            logger.exception("Video encoder worker crashed")

    def _encode_one_video(self, item: _VideoItem, stream) -> None:
        # Convert numpy array -> PyAV VideoFrame.
        # WGC delivers BGRA; that's what we declared on init. PyAV's
        # from_ndarray supports "bgra" (4 channel) and will reformat to the
        # stream's pix_fmt (yuv420p / nv12) at encode time.
        frame = av.VideoFrame.from_ndarray(item.array, format=self._video_pix_fmt)
        if self._needs_scale:
            # Downscale via libswscale into the target size + final pix_fmt.
            # Doing both at once is cheaper than scale → re-reformat at encode.
            frame = frame.reformat(
                width=self._target_width,
                height=self._target_height,
                format=self._encoder_pix_fmt,
            )
        # Timestamp in exact frame-rate slots. Wallclock-derived slots preserve
        # A/V alignment, while the submit guard prevents duplicate frame times.
        frame.pts = item.pts
        frame.time_base = Fraction(1, self._video_framerate)
        for packet in stream.encode(frame):
            with self._lock:
                if self._container is not None:
                    self._container.mux(packet)
        self._stats.video_frames_encoded += 1

    def _audio_worker(self) -> None:
        """Pull mic + system chunks; push to filter graph; pull mixed; encode."""
        assert self._audio_stream is not None
        assert self._audio_graph is not None
        assert self._audio_buf_mic is not None
        assert self._audio_buf_sys is not None
        assert self._audio_sink is not None
        stream = self._audio_stream

        try:
            while True:
                # Non-blocking drain of both input queues; the graph mixes
                # whichever arrives. If neither has anything, briefly wait.
                mic_pulled = (
                    False if self._mic_input_closed
                    else self._pull_into_graph(self._mic_q, self._audio_buf_mic, "mic")
                )
                sys_pulled = (
                    False if self._sys_input_closed
                    else self._pull_into_graph(self._sys_q, self._audio_buf_sys, "sys")
                )

                # Watchdog: EOF a leg that's starved while the other still flows,
                # so amix(duration=longest) doesn't deadlock into silence + growth.
                self._check_audio_stall()

                # Drain whatever the graph can mix right now.
                self._drain_audio_sink(stream)

                # If we got nothing on this iteration and we're stopping, exit.
                if not mic_pulled and not sys_pulled:
                    mic_done = self._mic_input_closed or self._mic_q.empty()
                    sys_done = self._sys_input_closed or self._sys_q.empty()
                    if self._stop_event.is_set() and mic_done and sys_done:
                        # Send EOF to the graph so it flushes, then drain.
                        self._close_audio_input("mic", self._audio_buf_mic)
                        self._close_audio_input("sys", self._audio_buf_sys)
                        self._drain_audio_sink(stream)
                        break
                    # Short sleep to avoid busy spinning when both queues
                    # are temporarily empty but not yet stopping.
                    time.sleep(0.005)
        except Exception as e:
            self._record_worker_failure(e, component="audio")
            logger.exception("Audio encoder worker crashed")

    def _drain_audio_sink(self, stream) -> None:
        """Pull every ready mixed frame from the sink and encode+mux it.

        Keeps the PTS the filter graph computed (it derives from the input PTS we
        anchored to wallclock, so audio lands at the right position relative to
        video), but routes each frame through _encode_and_mux_audio, which
        enforces monotonic PTS and guards the mux. A bad frame can no longer
        crash the worker — which would drop ALL further audio + fail finalize.
        """
        while True:
            try:
                out_frame = self._audio_sink.pull()
            except (av.error.BlockingIOError, av.error.EOFError):
                break
            except av.error.FFmpegError as e:
                # Defensive: an unexpected libav error from the sink must not
                # take down the whole worker. Stop draining this round; the next
                # iteration retries once more input has been pushed.
                if not self._audio_mux_error_logged:
                    logger.warning("Audio sink pull error (skipping): %s", e)
                    self._audio_mux_error_logged = True
                break
            self._encode_and_mux_audio(out_frame, stream)

    def _encode_and_mux_audio(self, out_frame, stream) -> None:
        """Encode + mux one mixed-audio frame, enforcing monotonic PTS.

        Drops a frame whose PTS steps backward or repeats (the amix late-anchor
        case) so the Matroska muxer never sees a non-monotonic timestamp and
        raises EINVAL. Guards the encode+mux as a last line of defence so one bad
        packet can't kill the audio worker.
        """
        pts = out_frame.pts
        if pts is not None:
            if self._last_audio_out_pts is not None and pts <= self._last_audio_out_pts:
                self._audio_pts_dropped += 1
                return
            self._last_audio_out_pts = pts
        try:
            for packet in stream.encode(out_frame):
                with self._lock:
                    if self._container is not None:
                        self._container.mux(packet)
        except av.error.FFmpegError as e:
            self._audio_mux_errors += 1
            if not self._audio_mux_error_logged:
                logger.warning(
                    "Audio encode/mux error (skipping packet; further "
                    "occurrences silenced): %s", e,
                )
                self._audio_mux_error_logged = True

    def _check_audio_stall(self) -> None:
        """EOF an audio leg that's stopped delivering while the other flows.

        With amix(duration=longest), a leg that stays open but stops feeding
        (e.g. a mic whose driver wedges without going inactive) makes amix emit
        NOTHING and buffer the flowing leg without bound — a silent track plus
        multi-GB RAM growth over a long recording. EOF'ing the starved leg lets
        amix fall through to the live one (degrade to one leg, not total loss).
        Only fires when BOTH legs are open: a single-leg stall has nothing to
        fall back to and doesn't buffer.
        """
        if self._mic_input_closed or self._sys_input_closed:
            return
        now = time.monotonic()
        mic_stale = now - self._mic_last_data_wall > _AUDIO_STALL_TIMEOUT_S
        sys_stale = now - self._sys_last_data_wall > _AUDIO_STALL_TIMEOUT_S
        if mic_stale and not sys_stale:
            logger.warning(
                "Mic audio stalled (no data >%.0fs) while system audio flows; "
                "closing the mic leg so mixing continues", _AUDIO_STALL_TIMEOUT_S,
            )
            self._close_audio_input("mic", self._audio_buf_mic)
        elif sys_stale and not mic_stale:
            logger.warning(
                "System audio stalled (no data >%.0fs) while mic flows; closing "
                "the system leg so mixing continues", _AUDIO_STALL_TIMEOUT_S,
            )
            self._close_audio_input("sys", self._audio_buf_sys)

    def _next_audio_pts(
        self, kind: str, chunk_pts_seconds: float, n_frames: int, sr: int
    ) -> int:
        """PTS (in samples) for the next audio chunk of ``kind`` ("mic"/"sys").

        Anchors the first chunk to wallclock (matching video's reference) then
        advances by sample count for smooth, sample-accurate spacing. Re-anchors
        FORWARD when the count has fallen more than ``_AUDIO_RESYNC_THRESHOLD_S``
        behind wallclock — the symptom of dropped capture samples — so audio
        can't creep ahead of video over a long recording. Forward-only keeps PTS
        monotonic (the muxer requires it); a dropped patch becomes a short
        silence gap rather than permanent desync.
        """
        anchored_attr = "_mic_anchored" if kind == "mic" else "_sys_anchored"
        count_attr = "_mic_pts_samples" if kind == "mic" else "_sys_pts_samples"
        wall_samples = int(max(0.0, chunk_pts_seconds + self._audio_offset_s) * sr)
        count = getattr(self, count_attr)
        if not getattr(self, anchored_attr):
            count = wall_samples
            setattr(self, anchored_attr, True)
        elif wall_samples - count > int(_AUDIO_RESYNC_THRESHOLD_S * sr):
            count = wall_samples
        pts = count
        setattr(self, count_attr, count + n_frames)
        return pts

    def _pull_into_graph(
        self,
        q: "queue.Queue[_AudioItem | None]",
        buf_filter,
        kind: str,
    ) -> bool:
        """Pull one chunk (if any) from queue into the filter-graph input."""
        try:
            item = q.get_nowait()
        except queue.Empty:
            return False
        if item is None:
            self._close_audio_input(kind, buf_filter)
            return True

        # Real chunk delivered — stamp this leg's liveness clock for the stall
        # watchdog. A stalled capture stops feeding its queue, so this timestamp
        # freezes once the bounded queue drains (~0.6s later), which is the
        # signal _check_audio_stall watches for.
        if kind == "mic":
            self._mic_last_data_wall = time.monotonic()
        else:
            self._sys_last_data_wall = time.monotonic()

        samples = item.array
        if samples.ndim == 1:
            samples = samples[:, None]
        n_frames = samples.shape[0]
        if kind == "mic":
            sr = self._mic_sample_rate
            layout = _channels_to_layout(self._mic_channels)
        else:
            sr = self._sys_sample_rate
            layout = _channels_to_layout(self._sys_channels)
        pts = self._next_audio_pts(kind, item.pts_seconds, n_frames, sr)

        # PyAV expects audio in planar shape (channels, frames) for "flt"
        # is packed, "fltp" is planar. We declared the abuffer as "flt", so
        # we need packed interleaved: shape (1, frames * channels).
        interleaved = samples.astype(np.float32, copy=False).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(interleaved, format="flt", layout=layout)
        frame.sample_rate = sr
        frame.pts = pts
        frame.time_base = Fraction(1, sr)
        try:
            buf_filter.push(frame)
        except Exception as e:
            logger.exception("Failed pushing %s audio into filter graph", kind)
            # Surface the crash through the same channel as video-encode
            # errors so Recorder.stop() can log and the recording isn't
            # silently audio-less from here on.
            self._record_worker_failure(e, component="audio")
            return False
        return True

    def _close_audio_input(self, kind: str, buf_filter) -> None:
        if kind == "mic":
            if self._mic_input_closed:
                return
            self._mic_input_closed = True
        else:
            if self._sys_input_closed:
                return
            self._sys_input_closed = True
        try:
            buf_filter.push(None)
            logger.debug("Encoder %s audio input closed for this recording", kind)
        except Exception:
            logger.exception("Failed closing %s audio input", kind)


def _channels_to_layout(n: int) -> str:
    if n == 1:
        return "mono"
    if n == 2:
        return "stereo"
    if n == 6:
        return "5.1"
    return f"{n}c"
