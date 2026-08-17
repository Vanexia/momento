"""Recording-safe update quiescence and handoff lifecycle checks."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config  # noqa: E402
from momento.core.game_watcher import ActiveGame  # noqa: E402
from momento.core.session import SessionManager  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


class FakeRecorder:
    def __init__(self) -> None:
        self.busy = False
        self.recording = False
        self.cancel_calls = 0
        self.start_calls = 0
        self.on_window_closed = None

    @property
    def is_busy(self) -> bool:
        return self.busy or self.recording

    @property
    def is_recording(self) -> bool:
        return self.recording

    def cancel_start(self) -> None:
        self.cancel_calls += 1

    def start(self, **_kwargs) -> None:
        self.start_calls += 1
        self.recording = True

    def stop(self):
        self.busy = False
        self.recording = False
        return None

    def current_position(self):
        return None


class FakeWatcher:
    def __init__(self) -> None:
        self.running = True
        self.initial_scan = True
        self.stop_calls = 0
        self.start_calls = 0
        self.stop_result = True
        self.on_game_start = None
        self.on_game_stop = None

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def stop(self) -> bool:
        self.stop_calls += 1
        self.running = False
        return self.stop_result

    def wait_initial_scan(self, timeout: float | None = None) -> bool:
        del timeout
        return self.initial_scan

    def update_known_games(self, _exes) -> None:
        return None

    def update_fullscreen_skip(self, _exes) -> None:
        return None

    def set_record_any_fullscreen(self, _enabled) -> None:
        return None

    def release_active_for_retry(self, _game, retry_after_s=None) -> None:
        del retry_after_s


def _make_session(tmp: Path) -> tuple[SessionManager, FakeRecorder, FakeWatcher]:
    recorder = FakeRecorder()
    watcher = FakeWatcher()
    config = dataclasses.replace(Config(), output_folder=tmp)
    return SessionManager(config, watcher=watcher, recorder=recorder), recorder, watcher


def _game() -> ActiveGame:
    return ActiveGame(exe_name="race.exe", pid=424242, exe_path=None)


def test_idle_session_quiesces_atomically(tmp: Path) -> None:
    session, recorder, watcher = _make_session(tmp)
    lease = session.acquire_update_quiescence()
    check("idle session grants update quiescence", lease is not None)
    assert lease is not None
    check("quiescence stops watcher before handoff", not watcher.running and watcher.stop_calls == 1)

    session._on_game_start(_game())
    check("game callback cannot begin while update lease is held", recorder.start_calls == 0 and not session._start_pending)

    lease.release()
    check("aborted handoff resumes prior monitoring state", watcher.running and watcher.start_calls == 1)
    check("game callbacks are accepted again after abort", not session._update_quiescing)


def test_committed_quiescence_never_resumes(tmp: Path) -> None:
    session, _recorder, watcher = _make_session(tmp)
    lease = session.acquire_update_quiescence()
    assert lease is not None
    lease.commit()
    lease.release()
    check("committed handoff keeps monitoring stopped", not watcher.running and watcher.start_calls == 0)
    session._on_game_start(_game())
    check("committed handoff permanently rejects new starts", not session._start_pending)


def test_busy_pipeline_always_wins(tmp: Path) -> None:
    cases = ("starter", "recording", "finalizer")
    for case in cases:
        session, recorder, watcher = _make_session(tmp)
        if case == "starter":
            session._start_pending = True
        elif case == "recording":
            recorder.recording = True
        else:
            session._finalizing = True
        lease = session.acquire_update_quiescence()
        check(f"{case} prevents update quiescence", lease is None)
        check(f"{case} keeps monitoring untouched", watcher.running and watcher.stop_calls == 0)


def test_watcher_stop_timeout_fails_closed(tmp: Path) -> None:
    session, _recorder, watcher = _make_session(tmp)
    watcher.stop_result = False
    lease = session.acquire_update_quiescence()
    check("watcher stop timeout denies update quiescence", lease is None)
    check(
        "failed watcher stop requests monitoring resume",
        watcher.running and watcher.start_calls == 1,
    )
    check(
        "failed quiescence releases its recording gate",
        not session._update_quiescing,
    )


def test_game_start_race_has_one_winner(tmp: Path) -> None:
    session, _recorder, watcher = _make_session(tmp)
    session._wait_for_window_and_start = lambda _game: None
    gate = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def acquire() -> None:
        gate.wait()
        outcome["lease"] = session.acquire_update_quiescence()

    def game_start() -> None:
        gate.wait()
        session._on_game_start(_game())

    first = threading.Thread(target=acquire)
    second = threading.Thread(target=game_start)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)
    lease = outcome.get("lease")
    update_won = lease is not None and not session._start_pending
    game_won = lease is None and session._start_pending
    check("game-start/update race produces exactly one owner", update_won != game_won)
    check("race never leaves monitoring stopped without a lease", watcher.running or update_won)
    if lease is not None:
        lease.release()
    else:
        session.pause_monitoring()


def test_initial_scan_is_required(tmp: Path) -> None:
    session, _recorder, watcher = _make_session(tmp)
    watcher.initial_scan = False
    check("update readiness waits for the watcher's initial game scan", not session.wait_initial_scan(timeout=0.01))
    watcher.initial_scan = True
    check("update readiness observes a completed initial game scan", session.wait_initial_scan(timeout=0.01))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_update_lifecycle_") as d:
        tmp = Path(d)
        test_idle_session_quiesces_atomically(tmp)
        test_committed_quiescence_never_resumes(tmp)
        test_busy_pipeline_always_wins(tmp)
        test_watcher_stop_timeout_fails_closed(tmp)
        test_game_start_race_has_one_winner(tmp)
        test_initial_scan_is_required(tmp)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
