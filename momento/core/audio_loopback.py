"""WASAPI loopback capture for system audio via PortAudio (PyAudioWPatch).

Replaces the previous ``soundcard`` backend (see :mod:`momento.core.mic_capture`
and :mod:`momento.core.audio_devices` for the why). The user picks an *output*
endpoint (Speakers, headset, HDMI sink, ...) and we capture its loopback.

IMPORTANT loopback reality: WASAPI loopback delivers frames only while the
endpoint's render engine is running. Whether an *idle* endpoint keeps delivering
silence frames is device-dependent — onboard codecs (Realtek) usually do, USB
Some USB audio devices power down and deliver nothing until audio plays.
So the capture loop must never block on a read (a blocked thread can't see
stop()), and idle gaps are synthesized as silence at real-time pace — which is
exactly what an idle output endpoint sounds like — so the encoder's mix graph
never starves. The loop is the stream's only owner: it polls availability and
closes the stream itself on exit. stop() must NEVER close the stream from
another thread — PortAudio is not safe against that and it crashed the whole
process (native access violation, no traceback) when a wedged blocking read
was "force-closed".

Public surface unchanged: ``LoopbackDevice``, ``list_loopback_devices``,
``resolve_loopback_device`` and ``LoopbackStreamer``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from momento.core import audio_devices
from momento.core.audio_devices import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE

logger = logging.getLogger(__name__)

_CHUNK_FRAMES = 960  # ~20 ms at 48 kHz
_OPEN_TIMEOUT_S = 5.0
# Poll cadence while no full chunk is available. Chunks are ~20 ms, so 5 ms
# keeps the loop responsive to stop() without meaningful CPU cost.
_POLL_INTERVAL_S = 0.005
# Idle time before we start synthesizing silence (a few chunks of slack so we
# never interleave synthetic chunks with marginally-late real ones).
_SYNTH_GAP_S = 0.06
# A gap larger than this is a clock jump (system suspend), not idleness —
# re-anchor instead of burst-backfilling hours of silence chunks.
_CLOCK_JUMP_S = 1.0


AudioSink = Callable[[np.ndarray, float], bool]


@dataclass(frozen=True)
class LoopbackDevice:
    """A playback endpoint we can loopback-capture from."""

    name: str  # human-readable, what the user sees in the dropdown
    id: str  # the output endpoint's friendly name, persisted in config


def list_loopback_devices() -> list[LoopbackDevice]:
    """All playback endpoints available as loopback sources, default first."""
    try:
        with audio_devices.pyaudio_session() as p:
            names = audio_devices.list_output_device_names(p)
    except Exception:
        logger.exception("Could not enumerate output endpoints")
        return []
    out: list[LoopbackDevice] = []
    for name, is_default in names:
        label = f"{name}  (default)" if is_default else name
        out.append(LoopbackDevice(name=label, id=name))
    return out


def resolve_loopback_device(name_or_id: str) -> LoopbackDevice | None:
    """Find a device by id (friendly name, preferred) or display name.

    Migrates a legacy soundcard render-endpoint id to the matching device.
    """
    if not name_or_id:
        return None
    devices = list_loopback_devices()
    for d in devices:
        bare = d.name.split("  (default)")[0]
        if d.id == name_or_id or d.name == name_or_id or bare == name_or_id:
            return d
    if audio_devices.looks_like_endpoint_id(name_or_id):
        migrated = audio_devices.friendly_name_for_endpoint_id(name_or_id)
        if migrated:
            for d in devices:
                bare = d.name.split("  (default)")[0]
                if bare == migrated or d.id == migrated:
                    logger.info("Migrated legacy loopback endpoint")
                    return d
    return None


class LoopbackStreamer:
    """Captures from one output endpoint's loopback and pushes to a sink."""

    def __init__(
        self,
        device_id: str,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ) -> None:
        self._device_key = device_id
        self._resolved_name = device_id
        self._sample_rate = sample_rate
        self._channels = channels
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._open_event = threading.Event()
        self._open_error: Exception | None = None
        self._sink: AudioSink | None = None
        self._started = False
        self._chunks_submitted = 0
        self._silence_chunks = 0
        self.on_capture_failed: Callable[[Exception], None] | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def chunks_submitted(self) -> int:
        """Chunks of real device audio submitted (excludes synthesized silence)."""
        return self._chunks_submitted

    @property
    def silence_chunks_submitted(self) -> int:
        """Silence chunks synthesized while the endpoint's render engine was idle."""
        return self._silence_chunks

    def start(self, sink: AudioSink) -> None:
        if self._started:
            raise RuntimeError("LoopbackStreamer already started")
        d = resolve_loopback_device(self._device_key)
        if d is None:
            raise ValueError("Selected playback device was not found")
        self._resolved_name = d.id

        self._sink = sink
        self._stop_event.clear()
        self._open_event.clear()
        self._open_error = None
        self._chunks_submitted = 0
        self._silence_chunks = 0
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="LoopbackCapture", daemon=True
        )
        self._capture_thread.start()
        opened = self._open_event.wait(timeout=_OPEN_TIMEOUT_S)
        if self._open_error is not None:
            self._stop_event.set()
            raise RuntimeError(
                "Could not open the selected playback device. It may be unavailable or in use."
            ) from None
        if not opened:
            self._stop_event.set()
            raise RuntimeError(
                f"The selected playback device did not open within {_OPEN_TIMEOUT_S:.0f}s"
            )
        self._started = True
        logger.info(
            "LoopbackStreamer started: sr=%d ch=%d",
            self._sample_rate, self._channels,
        )

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        self._stop_event.set()
        t = self._capture_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                # NEVER close the stream from here to "unblock" the thread —
                # PortAudio isn't safe against closing a stream another thread
                # is using; that force-close crashed the whole process (native
                # access violation, no traceback). The loop is poll-based and
                # owns its stream, so it can only still be here if the host API
                # itself is wedged — leak the daemon thread and move on.
                logger.error(
                    "LoopbackCapture thread did not exit within %.0fs; leaking it", timeout
                )
        self._capture_thread = None
        self._sink = None
        self._started = False
        logger.info(
            "LoopbackStreamer stopped (submitted %d chunks + %d synthesized-silence)",
            self._chunks_submitted, self._silence_chunks,
        )

    def _capture_loop(self) -> None:
        try:
            with audio_devices.pyaudio_session() as p:
                stream, native_ch, rate = audio_devices.open_input_stream(
                    p, self._resolved_name, self._sample_rate, self._channels,
                    _CHUNK_FRAMES, loopback=True,
                )
                try:
                    self._open_event.set()
                    sink = self._sink
                    chunk_s = _CHUNK_FRAMES / float(rate)
                    silence = np.zeros((_CHUNK_FRAMES, self._channels), dtype=np.float32)
                    last_data = time.monotonic()
                    # pts_seconds=None lets the encoder stamp the chunk with its
                    # own shared t0 — keeps mic / system audio / video aligned.
                    # Never a blocking read: an idle endpoint may deliver nothing
                    # forever, and a thread stuck inside Pa_ReadStream can't see
                    # stop(). Poll availability instead.
                    while not self._stop_event.is_set() and sink is not None:
                        if stream.get_read_available() >= _CHUNK_FRAMES:
                            raw = stream.read(_CHUNK_FRAMES, exception_on_overflow=False)
                            arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, native_ch)
                            data = audio_devices.to_channels(arr, self._channels)
                            last_data = time.monotonic()
                            if not self._submit(
                                sink, np.ascontiguousarray(data, dtype=np.float32)
                            ):
                                return
                            self._chunks_submitted += 1
                            continue
                        if not stream.is_active():
                            raise RuntimeError(
                                "loopback stream stopped delivering (device lost?)"
                            )
                        now = time.monotonic()
                        if now - last_data > _CLOCK_JUMP_S:
                            # System suspend / debugger pause — re-anchor rather
                            # than burst-backfilling the whole gap as silence.
                            last_data = now - _SYNTH_GAP_S
                        if now - last_data >= _SYNTH_GAP_S:
                            # Endpoint render engine is idle (nothing playing) —
                            # that IS silence; synthesize it at real-time pace so
                            # the encoder's mix graph keeps flowing.
                            last_data += chunk_s
                            if not self._submit(sink, silence):
                                return
                            self._silence_chunks += 1
                        else:
                            self._stop_event.wait(_POLL_INTERVAL_S)
                finally:
                    # The loop is the stream's only owner; nobody else may touch
                    # it (see stop()).
                    with contextlib.suppress(Exception):
                        stream.stop_stream()
                    with contextlib.suppress(Exception):
                        stream.close()
        except Exception as e:
            if not self._open_event.is_set():
                logger.warning(
                    "Failed to open selected loopback device (%s)", type(e).__name__
                )
                self._open_error = e
            else:
                logger.exception("Loopback capture loop crashed")
                self._notify_capture_failed(e)
        finally:
            self._open_event.set()

    def _submit(self, sink: AudioSink, data: np.ndarray) -> bool:
        """Push one chunk to the sink; False (after notifying) if the sink died."""
        try:
            sink(data, None)
            return True
        except Exception as e:
            logger.exception("Loopback sink raised; ending capture")
            self._notify_capture_failed(e)
            return False

    def _notify_capture_failed(self, exc: Exception) -> None:
        if self._stop_event.is_set():
            return
        cb = self.on_capture_failed
        if cb is None:
            return
        try:
            cb(exc)
        except Exception:
            logger.exception("Loopback capture-failure callback raised")


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or repr(exc)
