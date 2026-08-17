"""Deterministic GameWatcher stop and deferred-restart regression checks."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config  # noqa: E402
from momento.core.game_watcher import GameWatcher  # noqa: E402
from momento.core.session import SessionManager  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


class FakeRecorder:
    def __init__(self) -> None:
        self.on_window_closed = None
        self.on_audio_dropped = None
        self.on_encoder_failed = None
        self.on_video_capture_failed = None
        self.on_video_degraded = None

    @property
    def is_busy(self) -> bool:
        return False

    @property
    def is_recording(self) -> bool:
        return False

    def cancel_start(self) -> None:
        return None


class ResultWatcher(GameWatcher):
    def __init__(self) -> None:
        super().__init__(known_games=[], poll_interval=0.01)
        self.last_stop_result: bool | None = None

    def stop(self) -> bool:
        self.last_stop_result = super().stop()
        return self.last_stop_result


def test_timeout_fails_closed_and_resumes(tmp: Path) -> None:
    watcher = ResultWatcher()
    session = SessionManager(
        dataclasses.replace(Config(), output_folder=tmp),
        watcher=watcher,
        recorder=FakeRecorder(),
    )
    first_tick_entered = threading.Event()
    release_first_tick = threading.Event()
    replacement_tick_entered = threading.Event()
    tick_lock = threading.Lock()
    tick_count = 0
    tick_threads: set[int] = set()

    def controlled_tick() -> None:
        nonlocal tick_count
        with tick_lock:
            tick_count += 1
            tick_threads.add(threading.get_ident())
            current = tick_count
        if current == 1:
            first_tick_entered.set()
            release_first_tick.wait(timeout=5)
        else:
            replacement_tick_entered.set()

    watcher._tick = controlled_tick
    watcher.start()
    check("blocked watcher entered its callback", first_tick_entered.wait(timeout=1))

    lease = session.acquire_update_quiescence()
    check("watcher stop reports its timeout", watcher.last_stop_result is False)
    check("update quiescence fails closed while watcher thread lives", lease is None)

    # The failed acquisition requests monitoring resume before the old callback
    # can return. Releasing it must therefore create a fresh watcher run.
    if lease is not None:
        lease.release()
    release_first_tick.set()
    check(
        "deferred resume starts a replacement watcher",
        replacement_tick_entered.wait(timeout=1),
    )
    check("monitoring is live after the timed-out watcher exits", session.is_monitoring)
    check(
        "replacement watcher completed its initial scan",
        session.wait_initial_scan(timeout=1),
    )
    with tick_lock:
        thread_count = len(tick_threads)
    check("exactly one replacement watcher thread starts", thread_count == 2)

    check("replacement watcher stops cleanly", watcher.stop())
    check("repeated stop is idempotent", watcher.stop())


def test_normal_start_is_idempotent() -> None:
    watcher = GameWatcher(known_games=[], poll_interval=0.01)
    first_scan = threading.Event()
    tick_count = 0
    tick_lock = threading.Lock()

    def counted_tick() -> None:
        nonlocal tick_count
        with tick_lock:
            tick_count += 1
        first_scan.set()

    watcher._tick = counted_tick
    watcher.start()
    check("normal watcher performs an initial scan", first_scan.wait(timeout=1))
    first_thread = watcher._thread
    watcher.start()
    check("start is idempotent while running", watcher._thread is first_thread)
    check("normal watcher stop succeeds", watcher.stop())

    first_scan.clear()
    watcher.start()
    check("watcher starts again after a clean stop", first_scan.wait(timeout=1))
    check("clean restart uses a new thread", watcher._thread is not first_thread)
    check("final watcher stop succeeds", watcher.stop())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_watcher_lifecycle_") as d:
        test_timeout_fails_closed_and_resumes(Path(d))
    test_normal_start_is_idempotent()

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
