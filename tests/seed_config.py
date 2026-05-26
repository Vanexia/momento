"""Write a starter config.json so the tray app is usable before M7's settings UI.

Run once after install (or after a fresh delete of config.json) to drop a
config tailored to this machine's audio devices.

    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\seed_config.py `
        --mic "Mic In (Elgato Wave:XLR)" `
        --audio "Wave Link Monitor (Elgato Wave:XLR)" `
        --output C:\\dev\\Momento\\recordings `
        --extra-game notepad.exe
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config, DEFAULT_KNOWN_GAMES, save_config  # noqa: E402
from momento.core.audio_loopback import list_loopback_devices, resolve_loopback_device  # noqa: E402
from momento.util.paths import config_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mic", required=True, help="dshow capture device name")
    parser.add_argument(
        "--audio",
        help="WASAPI playback endpoint NAME (e.g. 'Speakers (Realtek(R) Audio)'). "
        "If omitted, the default speaker is used.",
    )
    parser.add_argument("--output", required=True, help="Output folder")
    parser.add_argument("--extra-game", action="append", default=[])
    parser.add_argument("--mic-vol", type=int, default=100)
    parser.add_argument("--audio-vol", type=int, default=100)
    args = parser.parse_args()

    devices = list_loopback_devices()
    if not devices:
        parser.error("No WASAPI playback endpoints found on this machine.")

    if args.audio:
        # Try exact name match first (with or without "  (default)" suffix).
        target = args.audio.strip()
        matched = None
        for d in devices:
            if d.name == target or d.name.rstrip(" (default)").strip() == target:
                matched = d
                break
        if matched is None:
            matched = resolve_loopback_device(args.audio)
        if matched is None:
            avail = "\n  ".join(d.name for d in devices)
            parser.error(f"Playback endpoint not found: {args.audio!r}\nAvailable:\n  {avail}")
        chosen_id = matched.id
        chosen_name = matched.name
    else:
        chosen_id = devices[0].id
        chosen_name = devices[0].name

    cfg = Config(
        mic_device=args.mic,
        system_audio_device=chosen_id,
        mic_volume_pct=args.mic_vol,
        system_volume_pct=args.audio_vol,
        output_folder=Path(args.output).resolve(),
        known_games=list(DEFAULT_KNOWN_GAMES) + list(args.extra_game),
    )
    Path(args.output).mkdir(parents=True, exist_ok=True)
    p = config_path()
    save_config(cfg, p)
    print(f"Wrote {p}")
    print(f"  mic    = {args.mic!r}")
    print(f"  system = {chosen_name!r}  id={chosen_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
