"""Live mic monitor for the Audio settings tab.

Independent of :class:`momento.core.mic_capture.MicStreamer` — the streamer
exists to feed the encoder, this exists so the user can hear themselves
and watch a level meter bounce while they pick the right device. Same
soundcard library, different lifetime, no encoder coupling.

Plays the mic through the system's default playback device, so the user
should ideally have headphones on — speakers will produce feedback. The
UI surfaces a warning where the button lives.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import soundcard as sc
from PyQt6.QtCore import QObject, pyqtSignal

from momento.core.mic_capture import resolve_mic_device

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000
_CHANNELS = 2
_BLOCK_FRAMES = 480  # ~10 ms at 48 kHz — keeps round-trip latency low


class MicMonitor(QObject):
    """Background mic reader. Emits :py:attr:`level_changed` continuously
    while running; if ``monitor_to_speaker`` is True the audio is also
    written to the default speaker for monitoring.

    Signals are Qt signals so emissions from the worker thread reach the
    Qt main thread via the default auto-connection.
    """

    level_changed = pyqtSignal(float)   # peak amplitude in [0, 1]
    error = pyqtSignal(str)             # human-readable failure reason
    stopped = pyqtSignal()              # fires once the worker thread exits

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._device_key: str = ""
        self._monitor_to_speaker = False

    # ------------------------------------------------------------------ API
    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self, mic_device_key: str, monitor_to_speaker: bool) -> None:
        """Start the monitor. If one is already running, restart with the
        new settings.

        ``mic_device_key`` accepts either a soundcard device id or a
        display name (same resolver as ``MicStreamer``).
        """
        self.stop()
        self._device_key = mic_device_key
        self._monitor_to_speaker = bool(monitor_to_speaker)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="MicMonitor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    # ------------------------------------------------------------- internals
    def _run(self) -> None:
        device = resolve_mic_device(self._device_key)
        if device is None:
            self.error.emit(
                "Mic device not found. Pick one from the dropdown and try again."
            )
            self.stopped.emit()
            return
        try:
            mic = sc.get_microphone(id=device.id, include_loopback=False)
        except Exception as e:
            logger.exception("MicMonitor: cannot open mic %s", device.id)
            self.error.emit(f"Couldn't open microphone: {e}")
            self.stopped.emit()
            return

        speaker = None
        if self._monitor_to_speaker:
            try:
                speaker = sc.default_speaker()
            except Exception:
                logger.exception("MicMonitor: cannot open default speaker")
                # Fall through — meter still works without playback.
                self.error.emit(
                    "No default speaker — meter will still update but you "
                    "won't hear yourself."
                )

        try:
            with mic.recorder(
                samplerate=_SAMPLE_RATE,
                channels=_CHANNELS,
                blocksize=_BLOCK_FRAMES,
            ) as recorder:
                player_ctx = (
                    speaker.player(
                        samplerate=_SAMPLE_RATE,
                        channels=_CHANNELS,
                        blocksize=_BLOCK_FRAMES,
                    )
                    if speaker is not None
                    else None
                )
                player = player_ctx.__enter__() if player_ctx is not None else None
                try:
                    while not self._stop.is_set():
                        data = recorder.record(numframes=_BLOCK_FRAMES)
                        if data.size:
                            peak = float(np.max(np.abs(data)))
                        else:
                            peak = 0.0
                        self.level_changed.emit(peak)
                        if player is not None:
                            try:
                                player.play(data)
                            except Exception:
                                logger.exception("MicMonitor: speaker write failed")
                                # Give up on playback but keep the meter going.
                                player = None
                                if player_ctx is not None:
                                    try:
                                        player_ctx.__exit__(None, None, None)
                                    except Exception:
                                        pass
                                    player_ctx = None
                finally:
                    if player_ctx is not None:
                        try:
                            player_ctx.__exit__(None, None, None)
                        except Exception:
                            pass
        except Exception as e:
            logger.exception("MicMonitor loop crashed")
            self.error.emit(f"Mic test stopped: {e}")
        finally:
            self.stopped.emit()
