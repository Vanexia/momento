"""Targeted tests for the window-discovery retry (2026-06-10 incident).

The bug: a known game whose window took >10s to appear (WoW on a patch day)
got ONE 10s wait_for_window attempt on the watcher thread; on failure the
session toasted "couldn't find a window" and never retried — the watcher pins
the game as active until process exit, so the entire session went unrecorded.

The fix moves window discovery to a RecordingStarter thread that keeps
retrying while the game process is alive (capped), starts recording the
moment the window appears, and aborts silently on process exit or paused
monitoring. The failure toast fires only at the cap.

Run:
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_window_retry.py
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import momento.core.session as sess  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.core.game_watcher import ActiveGame  # noqa: E402
from momento.core.session import (  # noqa: E402
    FAILURE_GENERIC, FAILURE_NO_WINDOW, FAILURE_OUTPUT_FOLDER,
    STATUS_RECORDING, SessionManager,
)

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


class FakeRecorder:
    def __init__(self) -> None:
        self.start_calls: list[dict] = []
        self._recording = False
        self.on_window_closed = None
        self.start_error: Exception | None = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_busy(self) -> bool:
        return self._recording

    def start(self, **kwargs) -> None:
        self.start_calls.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        self._recording = True

    def stop(self):
        self._recording = False
        return None

    def current_position(self):
        return None


class FakeWatcher:
    def __init__(self) -> None:
        self.is_running = True
        self.on_game_start = None
        self.on_game_stop = None
        self.released: list[tuple[int, float | None]] = []

    def update_known_games(self, exes) -> None:
        pass

    def set_record_any_fullscreen(self, enabled) -> None:
        pass

    def update_fullscreen_skip(self, exes) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def release_active_for_retry(self, game, retry_after_s=None) -> None:
        self.released.append((game.pid, retry_after_s))


def _make_session(tmp: Path) -> tuple[SessionManager, FakeRecorder, list, list]:
    cfg = dataclasses.replace(
        Config(),
        mic_device="fake-mic",
        system_audio_device="fake-sys",
        output_folder=tmp,
    )
    rec = FakeRecorder()
    s = SessionManager(cfg, watcher=FakeWatcher(), recorder=rec)
    failures: list[str] = []
    statuses: list[str] = []
    s.set_failure_callback(lambda reason, game, detail: failures.append(reason))
    s.set_status_callback(lambda status, game: statuses.append(status))
    return s, rec, failures, statuses


def _game() -> ActiveGame:
    return ActiveGame(exe_name="slowgame.exe", pid=987654, exe_path=None)


def test_slow_window_eventually_records(tmp: Path) -> None:
    """Window appears on the 3rd retry slice -> recording starts, no toast."""
    s, rec, failures, statuses = _make_session(tmp)
    attempts = {"n": 0}

    def fake_wait(pid, timeout=0.0, poll_interval=0.25):
        attempts["n"] += 1
        return 4242 if attempts["n"] >= 3 else None

    sess.wait_for_window = fake_wait
    sess._game_still_running = lambda game: True
    # Drive through the real entry point so the thread spawn is covered too.
    s._on_game_start(_game())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not rec.start_calls:
        time.sleep(0.02)
    check("slow window: recording started", len(rec.start_calls) == 1)
    check("slow window: started on the found hwnd",
          bool(rec.start_calls) and rec.start_calls[0].get("hwnd") == 4242)
    check("slow window: took multiple attempts", attempts["n"] >= 3)
    check("slow window: no failure toast", failures == [])
    check("slow window: recording status emitted", STATUS_RECORDING in statuses)


def test_cap_expiry_toasts_once(tmp: Path) -> None:
    """Window never appears, process stays alive -> exactly one NO_WINDOW."""
    s, rec, failures, _ = _make_session(tmp)
    sess.wait_for_window = lambda pid, timeout=0.0, poll_interval=0.25: None
    sess._game_still_running = lambda game: True
    s._wait_for_window_and_start(_game())  # synchronous for determinism
    check("cap expiry: exactly one NO_WINDOW failure", failures == [FAILURE_NO_WINDOW])
    check("cap expiry: recorder never started", rec.start_calls == [])
    check("cap expiry: active game released for retry", s._watcher.released == [(_game().pid, sess._START_RETRY_COOLDOWN_S)])


def test_process_death_aborts_silently(tmp: Path) -> None:
    """Game exits before its window appears -> no toast, no start."""
    s, rec, failures, _ = _make_session(tmp)
    sess.wait_for_window = lambda pid, timeout=0.0, poll_interval=0.25: None
    sess._game_still_running = lambda game: False
    s._wait_for_window_and_start(_game())
    check("process death: no failure toast", failures == [])
    check("process death: recorder never started", rec.start_calls == [])


def test_reused_pid_after_window_wait_aborts(tmp: Path) -> None:
    """A window result for a PID no longer belonging to the detected game is
    stale and must not start a recording of the replacement process."""
    s, rec, failures, _ = _make_session(tmp)
    sess.wait_for_window = lambda pid, timeout=0.0, poll_interval=0.25: 4242
    sess._game_still_running = lambda game: False
    s._wait_for_window_and_start(_game())
    check("PID reuse after window wait: no failure toast", failures == [])
    check("PID reuse after window wait: recorder never started", rec.start_calls == [])


def test_paused_monitoring_aborts(tmp: Path) -> None:
    """Tray Pause (or shutdown) mid-wait cancels the pending auto-start."""
    s, rec, failures, _ = _make_session(tmp)
    s._watcher.is_running = False
    sess.wait_for_window = lambda pid, timeout=0.0, poll_interval=0.25: 4242
    sess._game_still_running = lambda game: True
    s._wait_for_window_and_start(_game())
    check("paused: no start while monitoring stopped", rec.start_calls == [])
    check("paused: no failure toast", failures == [])


def test_generic_start_failure_releases_for_retry(tmp: Path) -> None:
    """Transient recorder-start failures should not pin the game forever."""
    s, rec, failures, _ = _make_session(tmp)
    rec.start_error = RuntimeError("WGC closed before first frame arrived")
    sess._game_still_running = lambda game: True
    s._start_recording(_game(), hwnd=4242)
    check("generic start failure: failure toast emitted", failures == [FAILURE_GENERIC])
    check("generic start failure: active game released", s._watcher.released == [(_game().pid, sess._START_RETRY_COOLDOWN_S)])


def test_output_folder_failure_does_not_retry_loop(tmp: Path) -> None:
    """User-action failures should not re-toast every cooldown interval."""
    s, rec, failures, _ = _make_session(tmp)
    rec.start_error = RuntimeError("Output folder D:\\Missing is not writable")
    s._start_recording(_game(), hwnd=4242)
    check("output folder failure: output failure emitted", failures == [FAILURE_OUTPUT_FOLDER])
    check("output folder failure: active game not released", s._watcher.released == [])


def test_duplicate_start_ignored_while_window_pending(tmp: Path) -> None:
    """A second start event should not spawn a second window-wait thread."""
    s, rec, failures, _ = _make_session(tmp)
    wait_entered = threading.Event()
    release_wait = threading.Event()
    attempts = {"n": 0}

    def fake_wait(pid, timeout=0.0, poll_interval=0.25):
        attempts["n"] += 1
        wait_entered.set()
        release_wait.wait(timeout=3.0)
        return None

    sess.wait_for_window = fake_wait
    sess._game_still_running = lambda game: False
    try:
        s._on_game_start(_game())
        s._on_game_start(_game())
        entered = wait_entered.wait(timeout=3.0)
        release_wait.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and s._start_pending:
            time.sleep(0.01)
        check("duplicate start: first wait thread ran", entered)
        check("duplicate start: only one wait attempt", attempts["n"] == 1)
        check("duplicate start: recorder never started", rec.start_calls == [])
        check("duplicate start: no failure toast", failures == [])
        check("duplicate start: pending flag cleared", not s._start_pending)
    finally:
        release_wait.set()


def test_concurrent_finalize_only_emits_once(tmp: Path) -> None:
    """Window-close and process-exit finalizers must not both drive idle UI."""
    s, rec, failures, statuses = _make_session(tmp)
    game = _game()
    final = tmp / "Game_2026-01-01_100000.mkv"
    final.write_bytes(b"final")
    rec._recording = True
    stop_started = threading.Event()
    release_stop = threading.Event()
    stop_calls = {"n": 0}
    finished: list[Path] = []

    def slow_stop():
        stop_calls["n"] += 1
        stop_started.set()
        release_stop.wait(timeout=5.0)
        rec._recording = False
        return final

    rec.stop = slow_stop  # type: ignore[method-assign]
    s.set_recording_finished_callback(lambda path: finished.append(Path(path)))
    with s._lock:
        s._current_game = game
        s._current_output = final

    t1 = threading.Thread(
        target=s._finalize,
        args=(game,),
        kwargs={"source": "window closed"},
        daemon=True,
    )
    t1.start()
    first_entered = stop_started.wait(timeout=3.0)
    s._finalize(game, source="process exit")
    release_stop.set()
    t1.join(timeout=3.0)

    check("finalize race: first finalizer entered stop", first_entered)
    check("finalize race: recorder stopped exactly once", stop_calls["n"] == 1)
    check("finalize race: no failure toast", failures == [])
    check("finalize race: one idle status emitted", statuses.count(sess.STATUS_IDLE) == 1)
    check("finalize race: one finished path emitted", finished == [final])


def main() -> int:
    real_wait = sess.wait_for_window
    real_alive = sess._game_still_running
    real_total = sess._WINDOW_WAIT_TOTAL_S
    real_slice = sess._WINDOW_WAIT_SLICE_S
    sess._WINDOW_WAIT_TOTAL_S = 0.15
    sess._WINDOW_WAIT_SLICE_S = 0.02
    try:
        with tempfile.TemporaryDirectory(prefix="momento_retry_") as d:
            tmp = Path(d)
            for fn in (
                test_slow_window_eventually_records,
                test_cap_expiry_toasts_once,
                test_process_death_aborts_silently,
                test_reused_pid_after_window_wait_aborts,
                test_paused_monitoring_aborts,
                test_generic_start_failure_releases_for_retry,
                test_output_folder_failure_does_not_retry_loop,
                test_duplicate_start_ignored_while_window_pending,
                test_concurrent_finalize_only_emits_once,
            ):
                try:
                    fn(tmp)
                except Exception as e:
                    check(f"{fn.__name__} raised unexpectedly: {e!r}", False)
    finally:
        sess.wait_for_window = real_wait
        sess._game_still_running = real_alive
        sess._WINDOW_WAIT_TOTAL_S = real_total
        sess._WINDOW_WAIT_SLICE_S = real_slice

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
