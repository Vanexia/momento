"""Live mic monitor for the Audio settings tab.

Independent of :class:`momento.core.mic_capture.MicStreamer` — the streamer
exists to feed the encoder, this exists so the user can hear themselves and
watch a level meter bounce while picking a device. Same PortAudio / WASAPI
backend (:mod:`momento.core.audio_devices`), different lifetime.

Plays the mic through the system's default playback device, so the user should
ideally have headphones on — speakers will produce feedback. The UI surfaces a
warning where the button lives.
"""

from __future__ import annotations

import contextlib
import logging
import threading

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from momento.core import audio_devices
from momento.core.mic_capture import resolve_mic_device

logger = logging.getLogger(__name__)

_CHANNELS = 2
_BLOCK_FRAMES = 480  # ~10 ms at 48 kHz — keeps round-trip latency low


class MicMonitor(QObject):
    """Background mic reader. Emits :py:attr:`level_changed` continuously while
    running; if ``monitor_to_speaker`` is True the audio is also written to the
    default speaker.

    Signals are Qt signals so emissions from the worker thread reach the Qt
    main thread via the default auto-connection.
    """

    level_changed = pyqtSignal(float)   # peak amplitude in [0, 1]
    error = pyqtSignal(str)             # human-readable failure reason
    stopped = pyqtSignal()              # fires once the worker thread exits

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        # A FRESH Event is created per run() (see start()) and captured locally
        # by the worker, so a slow-to-exit previous worker can never be revived
        # by the next start()'s event reset.
        self._stop = threading.Event()
        self._device_key: str = ""
        self._monitor_to_speaker = False

    # ------------------------------------------------------------------ API
    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self, mic_device_key: str, monitor_to_speaker: bool) -> None:
        """Start the monitor. If one is already running, restart with the new
        settings.

        ``mic_device_key`` accepts a soundcard-era endpoint id or a friendly
        name (same resolver as ``MicStreamer``).
        """
        self.stop()
        self._device_key = mic_device_key
        self._monitor_to_speaker = bool(monitor_to_speaker)
        # New Event each run: the previous worker (if still unwinding) holds a
        # reference to the OLD event, so it exits on its own and can't be
        # resurrected by this reset.
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._stop,), name="MicMonitor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    # ------------------------------------------------------------- internals
    def _run(self, stop: threading.Event) -> None:
        device = resolve_mic_device(self._device_key)
        if device is None:
            self.error.emit(
                "Mic device not found. Pick one from the dropdown and try again."
            )
            self.stopped.emit()
            return
        try:
            with audio_devices.pyaudio_session() as p:
                try:
                    # rate=0 -> the mic's native rate (PortAudio can't resample).
                    mic_stream, native_ch, rate = audio_devices.open_input_stream(
                        p, device.id, 0, _CHANNELS, _BLOCK_FRAMES
                    )
                except Exception as e:
                    logger.exception("MicMonitor: cannot open selected microphone")
                    self.error.emit(f"Couldn't open microphone: {_format_error(e)}")
                    return

                speaker = None
                if self._monitor_to_speaker:
                    out_name = audio_devices.default_output_name(p)
                    if out_name:
                        try:
                            # Open the speaker at the mic's rate so the
                            # pass-through plays at the right speed without
                            # resampling. If the endpoint won't take that rate,
                            # fall back to meter-only.
                            speaker, _sch, _sr = audio_devices.open_output_stream(
                                p, out_name, rate, _CHANNELS, _BLOCK_FRAMES
                            )
                        except Exception:
                            logger.exception("MicMonitor: cannot open default speaker")
                            self.error.emit(
                                "No matching speaker output — meter will still "
                                "update but you won't hear yourself."
                            )

                try:
                    while not stop.is_set():
                        try:
                            raw = mic_stream.read(
                                _BLOCK_FRAMES, exception_on_overflow=False
                            )
                        except Exception:
                            if stop.is_set():
                                break
                            raise
                        arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, native_ch)
                        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
                        self.level_changed.emit(peak)
                        if speaker is not None:
                            try:
                                out = audio_devices.to_channels(arr, _CHANNELS)
                                speaker.write(
                                    np.ascontiguousarray(out, dtype=np.float32).tobytes()
                                )
                            except Exception:
                                logger.exception("MicMonitor: speaker write failed")
                                with contextlib.suppress(Exception):
                                    speaker.stop_stream()
                                with contextlib.suppress(Exception):
                                    speaker.close()
                                speaker = None  # keep the meter going
                finally:
                    if speaker is not None:
                        with contextlib.suppress(Exception):
                            speaker.stop_stream()
                        with contextlib.suppress(Exception):
                            speaker.close()
                    with contextlib.suppress(Exception):
                        mic_stream.stop_stream()
                    with contextlib.suppress(Exception):
                        mic_stream.close()
        except Exception as e:
            logger.exception("MicMonitor loop crashed")
            self.error.emit(f"Mic test stopped: {_format_error(e)}")
        finally:
            self.stopped.emit()


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or repr(exc)
