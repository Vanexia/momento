"""End-to-end test of WGC window capture + WASAPI audio + mic via the Recorder.

Spawns Notepad, finds its HWND, records for 5s, exits cleanly. Then ffprobes.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402

from momento.core.audio_loopback import list_loopback_devices, resolve_loopback_device  # noqa: E402
from momento.core.recorder import Recorder  # noqa: E402
from momento.core.video_capture import wait_for_window  # noqa: E402
from momento.util.ffmpeg_path import ffprobe_exe  # noqa: E402
from momento.util.windows_api import find_main_hwnd_for_pid_with_children  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mic", required=True)
    parser.add_argument("--audio", help="WASAPI speaker NAME; default speaker if omitted")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--target-exe", default="notepad.exe",
                        help="Spawn this exe and capture its window")
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
    print(f"System audio: {d.name}")

    print(f"Spawning {args.target_exe} ...")
    target = subprocess.Popen(
        [args.target_exe],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )

    # Windows 11 Notepad: the launcher PID exits and a child Notepad.exe
    # owns the actual window. Mimic GameWatcher's behaviour and find by name.
    target_pid = target.pid
    deadline = time.time() + 5.0
    while time.time() < deadline:
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if (p.info.get("name") or "").lower() == args.target_exe.lower():
                    target_pid = p.info["pid"]
                    break
            except psutil.NoSuchProcess:
                continue
        if find_main_hwnd_for_pid_with_children(target_pid):
            break
        time.sleep(0.2)

    hwnd = wait_for_window(target_pid, timeout=5.0)
    if hwnd is None:
        print("FAIL: target window did not appear")
        try:
            target.kill()
        except Exception:
            pass
        return 3
    print(f"Capturing HWND={hwnd} (pid={target_pid})")

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    rec = Recorder()
    rec.start(
        output_path=out,
        hwnd=hwnd,
        mic_device=args.mic,
        audio_device=d.id,
    )
    print(f"Recording {args.seconds}s ...")
    time.sleep(args.seconds)
    rc = rec.stop()
    print(f"ffmpeg rc={rc}")

    # Kill the target exe and any of its children (Windows 11 Notepad spawns a child)
    try:
        proc = psutil.Process(target.pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        proc.kill()
    except Exception:
        pass
    try:
        target.kill()
    except Exception:
        pass

    if not out.exists() or rc != 0:
        print(f"FAIL: output missing or bad rc; file exists={out.exists()}")
        return 4

    size = out.stat().st_size
    print(f"Output: {out} ({size:,} bytes)")

    print("--- ffprobe ---")
    proc = subprocess.run(
        [str(ffprobe_exe()), "-hide_banner", "-loglevel", "error",
         "-show_streams", "-of", "default=noprint_wrappers=1", str(out)],
        capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(("codec_name=", "codec_type=", "width=", "height=", "duration=",
                            "nb_frames=", "sample_rate=", "channels=", "r_frame_rate=",
                            "bit_rate=")):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
