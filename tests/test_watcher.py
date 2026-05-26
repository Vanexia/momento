"""M4 verification: watch for known games and print start/stop events.

Usage (PowerShell):
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\test_watcher.py

    # Watch for arbitrary processes (useful for testing without a game installed):
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\test_watcher.py --watch notepad.exe,calc.exe

Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.game_watcher import ActiveGame, GameWatcher  # noqa: E402


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(description="GameWatcher event tester")
    parser.add_argument(
        "--watch",
        help="Override known-games list with a comma-separated set of exe names",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval seconds")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    known = None
    if args.watch:
        known = [s.strip() for s in args.watch.split(",") if s.strip()]
        print(f"Watch list overridden: {known}")

    def on_start(game: ActiveGame) -> None:
        print(f"[{_ts()}] START  {game.exe_name}  pid={game.pid}  exe={game.exe_path}")

    def on_stop(game: ActiveGame) -> None:
        print(f"[{_ts()}] STOP   {game.exe_name}  pid={game.pid}")

    watcher = GameWatcher(
        known_games=known,
        poll_interval=args.interval,
        on_game_start=on_start,
        on_game_stop=on_stop,
    )

    print(f"Watching {'overridden list' if args.watch else 'default known_games.json'} "
          f"every {args.interval:.1f}s. Press Ctrl-C to stop.")
    watcher.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        watcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
