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
from dataclasses import dataclass, field
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

    def summary(self) -> str:
        return (
            f"video: {self.video_frames_encoded}/{self.video_frames_submitted} "
            f"encoded (drops={self.video_frames_dropped}); "
            f"mic drops={self.mic_chunks_dropped}/{self.mic_chunks_submitted}; "
            f"sys drops={self.sys_chunks_dropped}/{self.sys_chunks_submitted}; "
            f"duration={self.duration_s:.2f}s"
        )


@dataclass
class _VideoItem:
    array: np.ndarray
    pts_seconds: float


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
        video_codec: str = "h264_nvenc",
        video_options: dict[str, str] | None = None,
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
                "the Recorder does the MKV->MP4 remux step itself."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path

        # Matroska muxer options. Smaller clusters trade a bit of file-size
        # overhead for crash recoverability — a hard kill (TerminateProcess,
        # power loss, BSOD) mid-write only loses the current cluster, which
        # is now at most ~1 second instead of libav's default ~5s. We also
        # request 64 KiB of reserved index space at the start so the SeekHead
        # has somewhere to live even if we don't get to call container.close().
        self._mkv_options = {
            "cluster_time_limit": "1000",   # ms — cap cluster duration
            "cluster_size_limit": "2097152", # 2 MiB — cap cluster size
            "reserve_index_space": "65536",  # 64 KiB pre-allocated for cues
        }

        self._video_width = int(video_width)
        self._video_height = int(video_height)
        self._video_framerate = int(video_framerate)
        self._video_pix_fmt = video_pix_fmt
        # Stream-output dimensions (post-scale). Default = source size, in
        # which case the per-frame reformat step is a no-op and libav just
        # swscales bgra → yuv420p at the native size.
        self._target_width = int(target_width) if target_width else self._video_width
        self._target_height = int(target_height) if target_height else self._video_height
        # Snap to even values — NVENC + yuv420p require it.
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
        self._video_options = dict(video_options) if video_options else {
            "preset": "p4",
            "tune": "hq",
            "rc": "vbr",
            "cq": "19",
            "b": "0",
            "spatial-aq": "1",
            "temporal-aq": "1",
        }
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

        # Stats
        self._stats = EncoderStats(output_path=self._path)
        self._t0_monotonic: float | None = None

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

    # ------------------------------------------------------------------ API
    @property
    def output_path(self) -> Path:
        return self._path

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop_event.is_set()

    @property
    def fatal_error(self) -> Exception | None:
        return self._fatal_error

    def start(self) -> None:
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
            # NVENC accepts NV12 natively; libav will swscale BGRA->NV12 at
            # encode time. We pick yuv420p as the on-disk pix_fmt because it
            # gives the broadest player compatibility once we remux to MP4.
            vs.pix_fmt = "yuv420p"
            vs.codec_context.options = self._video_options
            # Millisecond-resolution timebase. PTS values come from wallclock
            # seconds * 1000, so reordering / drift is easy to reason about.
            vs.time_base = Fraction(1, 1000)

            ass = container.add_stream(self._audio_codec_name, rate=self._mixed_sample_rate)
            ass.bit_rate = self._audio_bitrate
            # AAC always wants fltp internally; PyAV handles the conversion.
            self._video_stream = vs
            self._audio_stream = ass

            self._build_audio_graph()
        except Exception:
            try:
                container.close()
            except Exception:
                pass
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        self._container = container
        self._t0_monotonic = time.monotonic()
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
        if not self._started or self._stop_event.is_set():
            return False
        pts = pts_seconds if pts_seconds is not None else self._wallclock_pts()
        item = _VideoItem(array=frame, pts_seconds=pts)
        return self._push_drop_oldest(self._video_q, item, "video")

    def submit_mic_audio(self, samples: np.ndarray, pts_seconds: float | None = None) -> bool:
        """Submit a mic audio chunk. ``samples`` is shape (frames, channels), float32."""
        if not self._started or self._stop_event.is_set():
            return False
        pts = pts_seconds if pts_seconds is not None else self._wallclock_pts()
        item = _AudioItem(array=samples, pts_seconds=pts)
        return self._push_drop_oldest(self._mic_q, item, "mic")

    def submit_sys_audio(self, samples: np.ndarray, pts_seconds: float | None = None) -> bool:
        """Submit a system-audio chunk. Same shape conventions as mic."""
        if not self._started or self._stop_event.is_set():
            return False
        pts = pts_seconds if pts_seconds is not None else self._wallclock_pts()
        item = _AudioItem(array=samples, pts_seconds=pts)
        return self._push_drop_oldest(self._sys_q, item, "sys")

    def stop(self) -> EncoderStats:
        """Flush queues, close encoders, finalize the MKV file.

        Returns an EncoderStats snapshot. Safe to call multiple times.
        """
        if not self._started:
            return self._stats

        # Signal workers to drain and exit. Sentinel = None.
        self._stop_event.set()
        for q in (self._video_q, self._mic_q, self._sys_q):
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

        for t in (self._video_thread, self._audio_thread):
            if t is not None and t.is_alive():
                t.join(timeout=_DRAIN_TIMEOUT_S)

        # Flush encoders.
        with self._lock:
            if self._container is not None:
                try:
                    if self._video_stream is not None:
                        for pkt in self._video_stream.encode(None):
                            self._container.mux(pkt)
                except Exception:
                    logger.exception("Error flushing video encoder")
                try:
                    if self._audio_stream is not None:
                        for pkt in self._audio_stream.encode(None):
                            self._container.mux(pkt)
                except Exception:
                    logger.exception("Error flushing audio encoder")
                try:
                    self._container.close()
                except Exception:
                    logger.exception("Error closing output container")
                self._container = None

        self._started = False
        if self._t0_monotonic is not None:
            self._stats.duration_s = time.monotonic() - self._t0_monotonic
        logger.info("InProcessEncoder stopped: %s", self._stats.summary())
        return self._stats

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
            self._fatal_error = e
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
                format="yuv420p",
            )
        frame.pts = int(item.pts_seconds * 1000)  # ms timebase
        frame.time_base = stream.time_base
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
                mic_pulled = self._pull_into_graph(self._mic_q, self._audio_buf_mic, "mic")
                sys_pulled = self._pull_into_graph(self._sys_q, self._audio_buf_sys, "sys")

                # Try to pull mixed frames from the sink. Keep the PTS that
                # the filter graph computed — it derives from the input
                # PTS we anchored to wallclock, so the audio packets land
                # at the right position in the output container relative
                # to video.
                while True:
                    try:
                        out_frame = self._audio_sink.pull()
                    except av.error.BlockingIOError:
                        break
                    except av.error.EOFError:
                        break
                    for packet in stream.encode(out_frame):
                        with self._lock:
                            if self._container is not None:
                                self._container.mux(packet)

                # If we got nothing on this iteration and we're stopping, exit.
                if not mic_pulled and not sys_pulled:
                    if self._stop_event.is_set() and self._mic_q.empty() and self._sys_q.empty():
                        # Send EOF to the graph so it flushes.
                        try:
                            self._audio_buf_mic.push(None)
                        except Exception:
                            pass
                        try:
                            self._audio_buf_sys.push(None)
                        except Exception:
                            pass
                        # Drain the remainder.
                        while True:
                            try:
                                out_frame = self._audio_sink.pull()
                            except (av.error.BlockingIOError, av.error.EOFError):
                                break
                            for packet in stream.encode(out_frame):
                                with self._lock:
                                    if self._container is not None:
                                        self._container.mux(packet)
                        break
                    # Short sleep to avoid busy spinning when both queues
                    # are temporarily empty but not yet stopping.
                    time.sleep(0.005)
        except Exception as e:
            self._fatal_error = e
            logger.exception("Audio encoder worker crashed")

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
            # Sentinel — but only propagate EOF when stop_event is set.
            return False

        samples = item.array
        if samples.ndim == 1:
            samples = samples[:, None]
        n_frames, n_channels = samples.shape
        if kind == "mic":
            sr = self._mic_sample_rate
            layout = _channels_to_layout(self._mic_channels)
            if not self._mic_anchored:
                # Anchor this stream at (encoder-relative-wallclock + offset),
                # then increment cumulatively. The anchor matches video's
                # wallclock reference so an event happening at encoder time
                # T lands at audio PTS = T*sr (modulo offset).
                anchored = max(0.0, item.pts_seconds + self._audio_offset_s)
                self._mic_pts_samples = int(anchored * sr)
                self._mic_anchored = True
            pts = self._mic_pts_samples
            self._mic_pts_samples += n_frames
        else:
            sr = self._sys_sample_rate
            layout = _channels_to_layout(self._sys_channels)
            if not self._sys_anchored:
                anchored = max(0.0, item.pts_seconds + self._audio_offset_s)
                self._sys_pts_samples = int(anchored * sr)
                self._sys_anchored = True
            pts = self._sys_pts_samples
            self._sys_pts_samples += n_frames

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
            self._fatal_error = e
            return False
        return True


def _channels_to_layout(n: int) -> str:
    if n == 1:
        return "mono"
    if n == 2:
        return "stereo"
    if n == 6:
        return "5.1"
    return f"{n}c"
