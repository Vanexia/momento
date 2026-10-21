"""Quitting must not kill a finalise that is already flushing the MKV.

Recorder.stop() marks the recorder idle as soon as stopping begins, so a
shutdown that only checked the busy flag skipped finalising and let the
interpreter tear down the daemon finaliser mid-write. Real trigger: Windows
sign-out/shutdown closes the game and then Mementos back to back, or the user
quits from the tray within a few seconds of closing a game.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config  # noqa: E402
from momento.core.game_watcher import ActiveGame, GameWatcher  # noqa: E402
from momento.core import session as session_mod  # noqa: E402
from momento.core.session import SessionManager  # noqa: E402


class SlowStopRecorder:
    """Real Recorder ordering: idle is reported before the flush completes."""

    def __init__(self, output: Path) -> None:
        self.on_window_closed = None
        self.on_audio_dropped = None
        self.on_encoder_failed = None
        self.on_video_capture_failed = None
        self.on_video_degraded = None
        self._busy = True
        self._output = output
        self.stop_entered = threading.Event()
        self.release_flush = threading.Event()
        self.flush_completed = threading.Event()

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_recording(self) -> bool:
        return self._busy

    def cancel_start(self) -> None:
        return None

    def stop(self) -> Path:
        self._busy = False  # mirrors Recorder.stop() flipping _is_running first
        self.stop_entered.set()
        self.release_flush.wait(timeout=10)
        self.flush_completed.set()
        return self._output


class ShutdownTests(unittest.TestCase):
    def _session(self, tmp: Path, recorder: SlowStopRecorder) -> SessionManager:
        session = SessionManager(
            dataclasses.replace(Config(), output_folder=tmp),
            watcher=GameWatcher(known_games=[], poll_interval=0.01),
            recorder=recorder,
        )
        with session._lock:
            session._current_game = ActiveGame("game.exe", 4242, None, 1.0)
            session._current_output = recorder._output
        return session

    def test_shutdown_waits_for_an_in_flight_finalise(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mementos_shutdown_") as folder:
            tmp = Path(folder)
            recorder = SlowStopRecorder(tmp / "clip.mkv")
            session = self._session(tmp, recorder)

            finaliser = threading.Thread(
                target=session._finalize,
                args=(None,),
                kwargs={"source": "window closed"},
                daemon=True,
            )
            finaliser.start()
            self.assertTrue(recorder.stop_entered.wait(2))
            self.assertFalse(session._recorder_busy(), "precondition: recorder reads idle")

            shutdown_done = threading.Event()
            threading.Thread(
                target=lambda: (session.shutdown(), shutdown_done.set()), daemon=True
            ).start()
            time.sleep(0.3)
            self.assertFalse(shutdown_done.is_set(), "shutdown returned mid-flush")

            recorder.release_flush.set()
            self.assertTrue(shutdown_done.wait(5))
            self.assertTrue(recorder.flush_completed.is_set())

    def test_shutdown_wait_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mementos_shutdown_bound_") as folder:
            tmp = Path(folder)
            recorder = SlowStopRecorder(tmp / "clip.mkv")
            session = self._session(tmp, recorder)
            original = session_mod._FINALIZE_SHUTDOWN_WAIT_S
            session_mod._FINALIZE_SHUTDOWN_WAIT_S = 0.5
            try:
                threading.Thread(
                    target=session._finalize, args=(None,),
                    kwargs={"source": "process exit"}, daemon=True,
                ).start()
                self.assertTrue(recorder.stop_entered.wait(2))
                started = time.monotonic()
                session.shutdown()
                self.assertLess(time.monotonic() - started, 3.0)
            finally:
                session_mod._FINALIZE_SHUTDOWN_WAIT_S = original
                recorder.release_flush.set()

    def test_shutdown_without_a_finalise_does_not_wait(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mementos_shutdown_idle_") as folder:
            tmp = Path(folder)
            recorder = SlowStopRecorder(tmp / "clip.mkv")
            recorder._busy = False
            recorder.release_flush.set()
            session = SessionManager(
                dataclasses.replace(Config(), output_folder=tmp),
                watcher=GameWatcher(known_games=[], poll_interval=0.01),
                recorder=recorder,
            )
            started = time.monotonic()
            session.shutdown()
            self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
