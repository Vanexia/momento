"""Microphone capture via soundcard / WASAPI.

The old pipeline captured the mic through ffmpeg's dshow input, which forced
us to fight dshow's broken handling of device names that contain colons
(``Mic In (Elgato Wave:XLR)`` etc.) by resolving to the alternative-name
GUID. soundcard / WASAPI doesn't have that problem and gives us a consistent
device-enumeration story with system audio loopback (same library, same
ndarray format).

Like :class:`LoopbackStreamer`, the streamer pushes float32 frames to a sink
callable on a background thread. No subprocess, no TCP.
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
_CHUNK_FRAMES = 960  # ~20 ms at 48 kHz


AudioSink = Callable[[np.ndarray, float], bool]


@dataclass(frozen=True)
class MicDevice:
    name: str
    id: str


def list_mic_devices() -> list[MicDevice]:
    """All real microphones (not loopback)."""
    mics = sc.all_microphones(include_loopback=False)
    try:
        default_id = sc.default_microphone().id
    except Exception:
        default_id = None

    out: list[MicDevice] = []
    seen: set[str] = set()
    if default_id is not None:
        for m in mics:
            if m.id == default_id and m.id not in seen:
                out.append(MicDevice(name=f"{m.name}  (default)", id=str(m.id)))
                seen.add(m.id)
    for m in mics:
        if m.id not in seen:
            out.append(MicDevice(name=m.name, id=str(m.id)))
            seen.add(m.id)
    return out


def resolve_mic_device(name_or_id: str) -> MicDevice | None:
    """Find a mic by id (preferred) or display name. ``name`` matches the
    bare name (without the ``  (default)`` suffix) too."""
    if not name_or_id:
        return None
    for d in list_mic_devices():
        bare = d.name.split("  (default)")[0]
        if d.id == name_or_id or d.name == name_or_id or bare == name_or_id:
            return d
    return None


class MicStreamer:
    """Captures from one microphone and pushes float32 frames to a sink."""

    def __init__(
        self,
        device_id_or_name: str,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ) -> None:
        self._device_key = device_id_or_name
        self._sample_rate = sample_rate
        self._channels = channels
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sink: AudioSink | None = None
        self._started = False
        self._chunks_submitted = 0

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
        if self._started:
            raise RuntimeError("MicStreamer already started")
        d = resolve_mic_device(self._device_key)
        if d is None:
            raise ValueError(f"Mic device not found: {self._device_key!r}")
        self._device_id_resolved = d.id

        self._sink = sink
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="MicCapture", daemon=True
        )
        self._capture_thread.start()
        self._started = True
        logger.info(
            "MicStreamer started: device=%s sr=%d ch=%d",
            d.name, self._sample_rate, self._channels,
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
        logger.info("MicStreamer stopped (submitted %d chunks)", self._chunks_submitted)

    def _capture_loop(self) -> None:
        try:
            mic = sc.get_microphone(id=self._device_id_resolved, include_loopback=False)
        except Exception:
            logger.exception("Failed to open mic id=%s", self._device_id_resolved)
            return

        try:
            with mic.recorder(
                samplerate=self._sample_rate,
                channels=self._channels,
                blocksize=_CHUNK_FRAMES,
            ) as recorder:
                sink = self._sink
                # pts_seconds=None — encoder stamps with shared t0.
                while not self._stop_event.is_set() and sink is not None:
                    data = recorder.record(numframes=_CHUNK_FRAMES)
                    try:
                        sink(np.ascontiguousarray(data, dtype=np.float32), None)
                        self._chunks_submitted += 1
                    except Exception:
                        logger.exception("Mic sink raised; ending capture")
                        return
        except Exception:
            logger.exception("Mic capture loop crashed")
