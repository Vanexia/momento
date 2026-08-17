"""Verify the WASAPI loopback pipeline end-to-end via the Recorder.

Runs a 5-second recording using the WASAPI system-audio path, then prints
stream info so you can confirm audio actually flowed.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.audio_loopback import list_loopback_devices, resolve_loopback_device  # noqa: E402
from momento.core.recorder import Recorder  # noqa: E402
from momento.util.ffmpeg_path import ffprobe_exe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mic", required=True)
    parser.add_argument("--audio", help="WASAPI speaker NAME; default speaker if omitted")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    devs = list_loopback_devices()
    if args.audio:
        d = resolve_loopback_device(args.audio)
        if d is None:
            for cand in devs:
                if cand.name.startswith(args.audio):
                    d = cand
                    break
        if d is None:
            print(f"ERROR: WASAPI device {args.audio!r} not found")
            return 2
    else:
        d = devs[0]
    print(f"Using system audio: {d.name}  id={d.id}")

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    rec = Recorder()
    rec.start(
        output_path=out,
        mic_device=args.mic,
        audio_device=d.id,
    )
    print(f"Recording {args.seconds}s ...")
    time.sleep(args.seconds)
    rc = rec.stop()
    print(f"ffmpeg rc={rc}, file size: {out.stat().st_size if out.exists() else 'MISSING'} bytes")

    if not out.exists() or rc != 0:
        return 3

    print("--- ffprobe ---")
    proc = subprocess.run(
        [
            str(ffprobe_exe()),
            "-hide_banner", "-loglevel", "error",
            "-show_streams", "-of", "default=noprint_wrappers=1",
            str(out),
        ],
        capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(("codec_name=", "codec_type=", "width=", "height=", "duration=",
                            "nb_frames=", "sample_rate=", "channels=", "r_frame_rate=")):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
