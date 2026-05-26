"""Self-contained verification of the audio-device fix.

Runs a 3-second recording via the Recorder (using friendly device names) and
prints whether the output file is non-empty. No game watcher / SessionManager —
isolates the recorder + device-resolution path."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.recorder import Recorder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mic", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=int, default=3)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    rec = Recorder()
    print(f"Recording {args.seconds}s with friendly names; recorder will resolve to alt names.")
    rec.start(
        output_path=output,
        mic_device=args.mic,
        audio_device=args.audio,
    )
    time.sleep(args.seconds)
    rc = rec.stop()
    print(f"ffmpeg exit code: {rc}")

    if output.exists():
        size = output.stat().st_size
        print(f"Output: {output} ({size:,} bytes)")
        if size > 0:
            print("PASS")
            return 0
        print("FAIL: file is empty")
        return 2
    print("FAIL: file missing")
    return 3


if __name__ == "__main__":
    sys.exit(main())
