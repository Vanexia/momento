"""M5 verification: console-only SessionManager runner.

This wires the watcher + recorder end-to-end. Edit the config below (or pass
flags) so the mic/system-audio device names match your machine, then launch a
real game from the known list. The session will auto-start a recording and
auto-stop when the game exits.

Usage (PowerShell):

    # See what's available
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\list_devices.py

    # Run the session (Ctrl-C to exit). Override the watch list to test without
    # a real game by adding e.g. --extra-game notepad.exe.
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\test_session.py `
        --mic "Mic In (Elgato Wave:XLR)" `
        --audio "Wave Link Monitor (Elgato Wave:XLR)" `
        --output C:\\dev\\Momento\\recordings
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config  # noqa: E402
from momento.core.game_watcher import ActiveGame  # noqa: E402
from momento.core.session import SessionManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end SessionManager runner")
    parser.add_argument("--mic", required=True, help="Mic dshow device name")
    parser.add_argument("--audio", required=True, help="System audio dshow device name")
    parser.add_argument("--output", required=True, help="Folder to write MP4s into")
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=1440)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--mic-vol", type=int, default=100)
    parser.add_argument("--audio-vol", type=int, default=100)
    parser.add_argument("--display", type=int, default=0)
    parser.add_argument(
        "--extra-game",
        action="append",
        default=[],
        help="Add an exe name to the watch list (repeatable, e.g. notepad.exe)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    output_folder = Path(args.output).resolve()
    output_folder.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        mic_device=args.mic,
        system_audio_device=args.audio,
        mic_volume_pct=args.mic_vol,
        system_volume_pct=args.audio_vol,
        output_folder=output_folder,
        resolution=(args.width, args.height),
        framerate=args.fps,
        display_index=args.display,
    )
    cfg.known_games = list(cfg.known_games) + list(args.extra_game)

    def on_status(status: str, game: ActiveGame | None) -> None:
        if game is None:
            print(f"  [status] {status}")
        else:
            print(f"  [status] {status} — {game.exe_name} pid={game.pid}")

    sess = SessionManager(cfg, on_status_change=on_status)

    stop_evt = threading.Event()

    def _sigint(_signum, _frame):
        print("\n  (signal) shutting down ...")
        stop_evt.set()

    signal.signal(signal.SIGINT, _sigint)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _sigint)  # type: ignore[attr-defined]

    print(f"Session running. Output folder: {output_folder}")
    print(f"Mic   : {cfg.mic_device}")
    print(f"Audio : {cfg.system_audio_device}")
    print(f"Watch : {cfg.known_games}")
    print("Launch a known game to start recording; close it to stop. Ctrl-C to exit.")

    sess.start()
    try:
        while not stop_evt.is_set():
            time.sleep(0.2)
    finally:
        sess.shutdown()
        print("Session shut down cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
