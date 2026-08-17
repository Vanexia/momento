"""Self-contained smoke test for GameWatcher: spawns notepad.exe, expects START,
kills it, expects STOP. Exits 0 on success, non-zero on failure."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.game_watcher import ActiveGame, GameWatcher  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    starts: list[ActiveGame] = []
    stops: list[ActiveGame] = []
    start_evt = threading.Event()
    stop_evt = threading.Event()

    def on_start(g: ActiveGame) -> None:
        starts.append(g)
        start_evt.set()

    def on_stop(g: ActiveGame) -> None:
        stops.append(g)
        stop_evt.set()

    watcher = GameWatcher(
        known_games=["notepad.exe"],
        poll_interval=0.5,
        on_game_start=on_start,
        on_game_stop=on_stop,
    )
    watcher.start()

    try:
        print("Spawning notepad.exe ...")
        proc = subprocess.Popen(
            ["notepad.exe"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW,
        )

        if not start_evt.wait(timeout=5):
            print("FAIL: START callback did not fire within 5s")
            try:
                proc.kill()
            except Exception:
                pass
            return 2

        print(f"OK START fired: {starts[0]}")

        # Kill the actual tracked process (modern Notepad's launcher and UI may
        # be separate processes; the watcher tracked whichever one psutil saw
        # first by basename).
        tracked_pid = starts[0].pid
        print(f"Killing tracked Notepad pid={tracked_pid} ...")
        try:
            psutil.Process(tracked_pid).kill()
        except psutil.NoSuchProcess:
            pass
        try:
            proc.kill()
        except Exception:
            pass

        if not stop_evt.wait(timeout=5):
            print("FAIL: STOP callback did not fire within 5s")
            return 3

        print(f"OK STOP fired:  {stops[0]}")
        print("smoke test passed")
        return 0
    finally:
        watcher.stop()


if __name__ == "__main__":
    sys.exit(main())
