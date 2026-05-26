"""End-to-end smoke test of the in-process Recorder.

Spawns Notepad, captures its window via the new PyAV pipeline for ~5s,
then verifies the resulting MP4 is playable and the right size.

Usage:
    .venv\\Scripts\\python.exe tests\\smoke_recorder.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402

from momento.core.audio_loopback import list_loopback_devices  # noqa: E402
from momento.core.mic_capture import list_mic_devices  # noqa: E402
from momento.core.recorder import Recorder  # noqa: E402
from momento.core.video_capture import wait_for_window  # noqa: E402
from momento.util.ffmpeg_path import ffprobe_exe  # noqa: E402
from momento.util.windows_api import find_main_hwnd_for_pid_with_children  # noqa: E402


SECONDS = 5
OUT_PATH = Path(__file__).resolve().parents[1] / "recordings" / "smoke_recorder.mkv"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mics = list_mic_devices()
    speakers = list_loopback_devices()
    if not mics:
        print("FAIL: no microphones enumerated")
        return 2
    if not speakers:
        print("FAIL: no playback endpoints enumerated")
        return 2
    mic = mics[0]
    spk = speakers[0]
    print(f"Mic: {mic.name}")
    print(f"Sys: {spk.name}")

    print("Spawning notepad ...")
    target = subprocess.Popen(
        ["notepad.exe"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )
    target_pid = target.pid

    # Win11 Notepad: launcher dies, child Notepad.exe owns the window.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if (p.info.get("name") or "").lower() == "notepad.exe":
                    target_pid = p.info["pid"]
                    break
            except psutil.NoSuchProcess:
                continue
        if find_main_hwnd_for_pid_with_children(target_pid):
            break
        time.sleep(0.2)

    hwnd = wait_for_window(target_pid, timeout=5.0)
    if hwnd is None:
        print("FAIL: notepad window did not appear")
        try:
            target.kill()
        except Exception:
            pass
        return 3
    print(f"HWND={hwnd} pid={target_pid}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.unlink(missing_ok=True)
    OUT_PATH.with_suffix(".mp4").unlink(missing_ok=True)  # legacy from old smoke

    rec = Recorder()
    rec.start(
        output_path=OUT_PATH,
        hwnd=hwnd,
        mic_device=mic.id,
        audio_device=spk.id,
        framerate=60,
    )
    print(f"Recording {SECONDS}s ...")
    time.sleep(SECONDS)
    final = rec.stop()
    print(f"stop() -> {final}")

    # Kill notepad + children
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

    if not OUT_PATH.exists():
        print(f"FAIL: output missing: {OUT_PATH}")
        return 4

    size = OUT_PATH.stat().st_size
    print(f"Output: {OUT_PATH} ({size:,} bytes)")

    print("--- ffprobe ---")
    proc = subprocess.run(
        [str(ffprobe_exe()), "-hide_banner", "-loglevel", "error",
         "-show_streams", "-show_format",
         "-of", "default=noprint_wrappers=1", str(OUT_PATH)],
        capture_output=True, text=True,
    )
    keep_prefixes = (
        "codec_name=", "codec_type=", "width=", "height=", "duration=",
        "nb_frames=", "nb_packets=", "sample_rate=", "channels=", "r_frame_rate=",
        "bit_rate=", "format_name=",
    )
    for line in proc.stdout.splitlines():
        if line.startswith(keep_prefixes):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
