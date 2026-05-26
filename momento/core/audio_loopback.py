"""WASAPI loopback capture for system audio.

Windows' DirectShow can't enumerate playback endpoints, so the old "system
audio" picker could only show capture devices (Stereo Mix, virtual cables).
WASAPI loopback fixes that — every output endpoint (Speakers, headset, HDMI
sink, ...) can be captured directly.

Implementation:
  * ``soundcard`` (pure-Python ctypes binding to Windows audio APIs) does the
    capture as float32 numpy arrays shaped (frames, channels).
  * A background thread reads those arrays and forwards them to a sink
    callable (typically ``InProcessEncoder.submit_sys_audio``).

Compared to the original TCP+ffmpeg path: no socket, no s16le conversion,
no subprocess hand-off. The capture thread calls the sink directly with the
float32 array soundcard produced. The encoder's submit method is
non-blocking (drop-oldest queue), so an overloaded encoder never stalls
capture.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import soundcard as sc

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
# ~20 ms of frames at 48 kHz — low enough latency, big enough not to hammer
# the GIL with one callback per frame.
_CHUNK_FRAMES = 960


# Sink signature: (float32_samples_frames_x_channels, pts_seconds) -> bool
AudioSink = Callable[[np.ndarray, float], bool]


@dataclass(frozen=True)
class LoopbackDevice:
    """A playback endpoint we can loopback-capture from."""

    name: str  # human-readable, what the user sees in the dropdown
    id: str  # stable across runs; what we persist in config


def list_loopback_devices() -> list[LoopbackDevice]:
    """Return all playback endpoints available as loopback sources.

    Default playback device is listed first.
    """
    speakers = sc.all_speakers()
    try:
        default_id = sc.default_speaker().id
    except Exception:
        default_id = None

    out: list[LoopbackDevice] = []
    seen_ids: set[str] = set()

    if default_id is not None:
        for s in speakers:
            if s.id == default_id and s.id not in seen_ids:
                out.append(LoopbackDevice(name=f"{s.name}  (default)", id=str(s.id)))
                seen_ids.add(s.id)

    for s in speakers:
        if s.id not in seen_ids:
            out.append(LoopbackDevice(name=s.name, id=str(s.id)))
            seen_ids.add(s.id)
    return out


def resolve_loopback_device(name_or_id: str) -> LoopbackDevice | None:
    """Find a device by id (preferred) or display name."""
    if not name_or_id:
        return None
    for d in list_loopback_devices():
        if d.id == name_or_id or d.name == name_or_id:
            return d
    return None


class LoopbackStreamer:
    """Captures from one output endpoint and pushes samples to a sink.

    Lifecycle:
        1. ``start(sink)`` — opens the device, spawns capture thread
        2. (capture thread feeds the sink continuously)
        3. ``stop()``     — signals exit, joins the thread
    """

    def __init__(
        self,
        device_id: str,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ) -> None:
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._channels = channels
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sink: AudioSink | None = None
        self._started = False
        self._chunks_submitted = 0

    # ---------------------------------------------------------- public API
    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def chunks_submitted(self) -> int:
        return self._chunks_submitted

    def start(self, sink: AudioSink) -> None:
        """Start capture. ``sink`` is called from the capture thread with
        each ~20 ms chunk."""
        if self._started:
            raise RuntimeError("LoopbackStreamer already started")
        if resolve_loopback_device(self._device_id) is None:
            raise ValueError(f"Loopback device not found: {self._device_id!r}")

        self._sink = sink
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="LoopbackCapture", daemon=True
        )
        self._capture_thread.start()
        self._started = True
        logger.info(
            "LoopbackStreamer started: device=%s sr=%d ch=%d",
            self._device_id, self._sample_rate, self._channels,
        )

    def stop(self, timeout: float = 3.0) -> None:
        if not self._started:
            return
        self._stop_event.set()
        if self._capture_thread is not None and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=timeout)
        self._capture_thread = None
        self._sink = None
        self._started = False
        logger.info("LoopbackStreamer stopped (submitted %d chunks)", self._chunks_submitted)

    # -------------------------------------------------------------- worker
    def _capture_loop(self) -> None:
        try:
            mic = sc.get_microphone(id=self._device_id, include_loopback=True)
        except Exception:
            logger.exception("Failed to open loopback microphone id=%s", self._device_id)
            return

        try:
            with mic.recorder(
                samplerate=self._sample_rate,
                channels=self._channels,
                blocksize=_CHUNK_FRAMES,
            ) as recorder:
                sink = self._sink
                # pts_seconds=None lets the encoder stamp the chunk with its
                # own shared t0 — keeps mic / system audio / video aligned
                # under one wallclock reference.
                while not self._stop_event.is_set() and sink is not None:
                    data = recorder.record(numframes=_CHUNK_FRAMES)
                    try:
                        sink(np.ascontiguousarray(data, dtype=np.float32), None)
                        self._chunks_submitted += 1
                    except Exception:
                        logger.exception("Loopback sink raised; ending capture")
                        return
        except Exception:
            logger.exception("Loopback capture loop crashed")
