"""Microphone capture via PortAudio / WASAPI (PyAudioWPatch).

Replaces the previous ``soundcard`` backend, which crashed (bare
``AssertionError``) on any device whose WASAPI mix format was reported as
``WAVE_FORMAT_IEEE_FLOAT`` rather than the extensible wrapper on some USB
microphone endpoints. PortAudio negotiates the format through WASAPI, so
any microphone Windows exposes records. See :mod:`momento.core.audio_devices`
for the shared device layer and the legacy-config migration.

The public surface is unchanged: ``MicDevice``, ``list_mic_devices``,
``resolve_mic_device`` and ``MicStreamer`` (which pushes float32 frames shaped
``(frames, channels)`` to a sink, exactly as before), so the recorder, settings
UI and status panel need no changes. The stream is opened at the device's native
rate (PortAudio shared mode can't resample); the recorder tells the encoder that
rate so its filter graph aresamples to 48 kHz.
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
# How long start() waits for the capture thread to actually open the device
# before declaring failure. WASAPI open is near-instant; this is a generous
# ceiling so a device that enumerates but won't open fails fast instead of
# leaving the recording silently without this stream.
_OPEN_TIMEOUT_S = 5.0
# Poll cadence while no full chunk is available (~20 ms chunks; 5 ms keeps the
# loop responsive to stop() without meaningful CPU cost).
_POLL_INTERVAL_S = 0.005
# A healthy mic delivers continuously; log (once per stall) if one goes quiet.
_STALL_WARN_S = 10.0


AudioSink = Callable[[np.ndarray, float], bool]


@dataclass(frozen=True)
class MicDevice:
    name: str  # human-readable, what the user sees in the dropdown
    id: str  # the friendly name, persisted in config; stable across runs


def list_mic_devices() -> list[MicDevice]:
    """All real microphones (not loopback). Default device first."""
    try:
        with audio_devices.pyaudio_session() as p:
            names = audio_devices.list_input_device_names(p)
    except Exception:
        logger.exception("Could not enumerate microphones")
        return []
    out: list[MicDevice] = []
    for name, is_default in names:
        label = f"{name}  (default)" if is_default else name
        out.append(MicDevice(name=label, id=name))
    return out


def resolve_mic_device(name_or_id: str) -> MicDevice | None:
    """Find a mic by id (friendly name, preferred) or display name.

    Also migrates a legacy soundcard WASAPI endpoint id ("{0.0.1...}.{guid}")
    to the matching device so an upgraded config keeps its selection.
    """
    if not name_or_id:
        return None
    devices = list_mic_devices()
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
                    logger.info("Migrated mic endpoint id %s -> %r", name_or_id, migrated)
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
        self._resolved_name = device_id_or_name
        self._sample_rate = sample_rate
        self._channels = channels
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Set by the capture loop once the device is open (or has failed to
        # open). start() waits on this so it can report the real result instead
        # of returning success the instant the thread is spawned.
        self._open_event = threading.Event()
        self._open_error: Exception | None = None
        self._sink: AudioSink | None = None
        self._started = False
        self._chunks_submitted = 0
        self.on_capture_failed: Callable[[Exception], None] | None = None

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
        self._resolved_name = d.id

        self._sink = sink
        self._stop_event.clear()
        self._open_event.clear()
        self._open_error = None
        self._chunks_submitted = 0
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="MicCapture", daemon=True
        )
        self._capture_thread.start()
        # Block until the capture thread has actually opened the device (or
        # failed to). Without this, start() returns success before the WASAPI
        # open even happens — so a device that enumerates but can't open (in
        # use, driver glitch, unplugged after enumeration) would leave the
        # recording silently without mic while the session reports "recording".
        opened = self._open_event.wait(timeout=_OPEN_TIMEOUT_S)
        # Prefer the real open error over the generic timeout message — it names
        # the actual reason (e.g. "device in use"), which is the whole point of
        # surfacing open failures.
        if self._open_error is not None:
            self._stop_event.set()
            raise RuntimeError(
                f"Could not open mic device {d.name!r}: {_format_error(self._open_error)}"
            ) from self._open_error
        if not opened:
            self._stop_event.set()
            raise RuntimeError(
                f"Mic device {d.name!r} did not open within {_OPEN_TIMEOUT_S:.0f}s"
            )
        self._started = True
        logger.info(
            "MicStreamer started: device=%s sr=%d ch=%d",
            self._resolved_name, self._sample_rate, self._channels,
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
                    "MicCapture thread did not exit within %.0fs; leaking it", timeout
                )
        self._capture_thread = None
        self._sink = None
        self._started = False
        logger.info("MicStreamer stopped (submitted %d chunks)", self._chunks_submitted)

    def _capture_loop(self) -> None:
        try:
            with audio_devices.pyaudio_session() as p:
                stream, native_ch, _rate = audio_devices.open_input_stream(
                    p, self._resolved_name, self._sample_rate, self._channels,
                    _CHUNK_FRAMES, loopback=False,
                )
                try:
                    # Device is open and recording — release start().
                    self._open_event.set()
                    sink = self._sink
                    last_data = time.monotonic()
                    stall_logged = False
                    # pts_seconds=None — encoder stamps with shared t0.
                    # Never a blocking read: a silently-stalling driver delivers
                    # nothing forever, and a thread stuck inside Pa_ReadStream
                    # can't see stop(). Poll availability instead. Unlike the
                    # loopback (where idle IS silence), a quiet mic is a device
                    # fault — don't synthesize; warn so the stall is diagnosable.
                    while not self._stop_event.is_set() and sink is not None:
                        if stream.get_read_available() >= _CHUNK_FRAMES:
                            raw = stream.read(_CHUNK_FRAMES, exception_on_overflow=False)
                            arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, native_ch)
                            data = audio_devices.to_channels(arr, self._channels)
                            last_data = time.monotonic()
                            stall_logged = False
                            try:
                                sink(np.ascontiguousarray(data, dtype=np.float32), None)
                                self._chunks_submitted += 1
                            except Exception as e:
                                logger.exception("Mic sink raised; ending capture")
                                self._notify_capture_failed(e)
                                return
                            continue
                        if not stream.is_active():
                            raise RuntimeError("mic stream stopped delivering (device lost?)")
                        if not stall_logged and time.monotonic() - last_data >= _STALL_WARN_S:
                            logger.warning(
                                "Mic %r has delivered no audio for %.0fs (driver stall?)",
                                self._resolved_name, _STALL_WARN_S,
                            )
                            stall_logged = True
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
                # Failed during device open — hand the error to start() so it
                # raises instead of reporting a phantom-started stream.
                logger.exception("Failed to open mic %r", self._device_key)
                self._open_error = e
            else:
                # Crash after a successful open is a mid-recording failure.
                logger.exception("Mic capture loop crashed")
                self._notify_capture_failed(e)
        finally:
            # Always release start(), even on an unexpected early exit.
            self._open_event.set()

    def _notify_capture_failed(self, exc: Exception) -> None:
        if self._stop_event.is_set():
            return
        cb = self.on_capture_failed
        if cb is None:
            return
        try:
            cb(exc)
        except Exception:
            logger.exception("Mic capture-failure callback raised")


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or repr(exc)
