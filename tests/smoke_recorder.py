"""End-to-end smoke test of the in-process Recorder.

Spawns a dedicated test window, captures it via the PyAV pipeline for ~5s,
then verifies the resulting MKV is playable and the right size.

Usage:
    .venv\\Scripts\\python.exe tests\\smoke_recorder.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

import av

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402

from momento.core.audio_loopback import list_loopback_devices  # noqa: E402
from momento.core.mic_capture import list_mic_devices  # noqa: E402
from momento.core.recorder import Recorder  # noqa: E402
from momento.core.video_capture import wait_for_window  # noqa: E402
from momento.util.ffmpeg_path import ffprobe_exe  # noqa: E402


SECONDS = 5
OUT_PATH = Path(__file__).resolve().parents[1] / "recordings" / "smoke_recorder.mkv"
WINDOW_SCRIPT = """
import tkinter as tk

root = tk.Tk()
root.title("Momento Capture Test")
root.geometry("960x540")
canvas = tk.Canvas(root, background="#17151d", highlightthickness=0)
canvas.pack(fill="both", expand=True)
canvas.create_rectangle(90, 90, 870, 450, fill="#7c3aed", outline="#a78bfa", width=4)
canvas.create_text(480, 270, text="Momento WGC Test", fill="white", font=("Segoe UI", 32, "bold"))
root.mainloop()
"""


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

    print("Spawning dedicated capture window ...")
    target = subprocess.Popen(
        [sys.executable, "-c", WINDOW_SCRIPT],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )
    target_pid = target.pid

    hwnd = wait_for_window(target_pid, timeout=5.0)
    if hwnd is None:
        print("FAIL: dedicated capture window did not appear")
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

    # Kill the dedicated test window + children.
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
    if proc.returncode != 0:
        print(f"FAIL: ffprobe could not read the recording: {proc.stderr.strip()}")
        return 5
    keep_prefixes = (
        "codec_name=", "codec_type=", "width=", "height=", "duration=",
        "nb_frames=", "nb_packets=", "sample_rate=", "channels=", "r_frame_rate=",
        "bit_rate=", "format_name=",
    )
    for line in proc.stdout.splitlines():
        if line.startswith(keep_prefixes):
            print(f"  {line}")
    has_video = "codec_type=video" in proc.stdout
    has_audio = "codec_type=audio" in proc.stdout
    has_duration = any(
        line.startswith("duration=") and line != "duration=N/A"
        for line in proc.stdout.splitlines()
    )
    video_start_s = None
    video_end_s = None
    with av.open(str(OUT_PATH)) as container:
        decoded_video_frames = 0
        for decoded_frame in container.decode(video=0):
            decoded_video_frames += 1
            if decoded_frame.pts is not None:
                frame_start_s = float(decoded_frame.pts * decoded_frame.time_base)
                if video_start_s is None:
                    video_start_s = frame_start_s
                video_end_s = frame_start_s + 1.0 / 60.0
    audio_start_s = None
    audio_end_s = None
    with av.open(str(OUT_PATH)) as container:
        for decoded_frame in container.decode(audio=0):
            if decoded_frame.pts is not None:
                frame_start_s = float(decoded_frame.pts * decoded_frame.time_base)
                if audio_start_s is None:
                    audio_start_s = frame_start_s
                audio_end_s = (
                    frame_start_s
                    + decoded_frame.samples / decoded_frame.sample_rate
                )
    with av.open(str(OUT_PATH)) as container:
        stream = container.streams.video[0]
        video_dts = [packet.dts for packet in container.demux(stream) if packet.dts is not None]
    dts_is_strict = all(
        current > previous for previous, current in zip(video_dts, video_dts[1:])
    )
    print(f"  decoded_video_frames={decoded_video_frames}")
    print(f"  strictly_increasing_video_dts={dts_is_strict}")
    av_end_delta_s = (
        abs(video_end_s - audio_end_s)
        if video_end_s is not None and audio_end_s is not None
        else float("inf")
    )
    av_start_delta_s = (
        abs(video_start_s - audio_start_s)
        if video_start_s is not None and audio_start_s is not None
        else float("inf")
    )
    print(f"  audio_video_start_delta={av_start_delta_s:.3f}s")
    print(f"  audio_video_end_delta={av_end_delta_s:.3f}s")
    ok = (
        size > 0
        and has_video
        and has_audio
        and has_duration
        and decoded_video_frames >= SECONDS * 60 * 0.8
        and dts_is_strict
        and av_start_delta_s <= 0.15
        and av_end_delta_s <= 0.25
    )
    print(f"{'PASS' if ok else 'FAIL'} - recording decodes with paced video and audio")
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(main())
