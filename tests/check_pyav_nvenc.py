"""PyAV + h264_nvenc proof of concept.

Goal: encode a 5-second synthetic test pattern to MKV via h264_nvenc, then
remux the MKV to MP4 with +faststart. If this runs end-to-end the Tier 3
in-process pipeline is viable on this machine.

Usage:
    .venv\\Scripts\\python.exe tests\\check_pyav_nvenc.py
"""

from __future__ import annotations

import sys
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np


WIDTH = 1280
HEIGHT = 720
FPS = 60
DURATION_S = 5
OUT_DIR = Path(__file__).parent.parent / "recordings"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MKV_PATH = OUT_DIR / "pyav_nvenc_poc.mkv"
MP4_PATH = OUT_DIR / "pyav_nvenc_poc.mp4"


def make_test_frame(t: float) -> np.ndarray:
    """Solid-coloured moving gradient as BGRA bytes — visually distinct frames."""
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    phase = int(t * 60) & 0xFF
    img[:, :, 0] = (np.arange(WIDTH) + phase) & 0xFF       # B gradient
    img[:, :, 1] = (np.arange(HEIGHT)[:, None] + phase) & 0xFF  # G gradient
    img[:, :, 2] = phase                                    # R pulses
    return img  # BGR24


def encode_mkv() -> None:
    print(f"Encoding {DURATION_S}s @ {FPS}fps {WIDTH}x{HEIGHT} h264_nvenc -> {MKV_PATH}")
    container = av.open(str(MKV_PATH), mode="w", format="matroska")
    try:
        stream = container.add_stream("h264_nvenc", rate=FPS)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "yuv420p"  # NVENC will receive nv12 after auto-convert
        stream.codec_context.options = {
            "preset": "p4",
            "tune": "hq",
            "rc": "vbr",
            "cq": "19",
            "b": "0",
            "spatial-aq": "1",
            "temporal-aq": "1",
        }
        stream.time_base = Fraction(1, FPS)

        n_frames = DURATION_S * FPS
        wall_start = time.perf_counter()
        for i in range(n_frames):
            arr = make_test_frame(i / FPS)
            frame = av.VideoFrame.from_ndarray(arr, format="bgr24")
            frame.pts = i
            frame.time_base = Fraction(1, FPS)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):  # flush
            container.mux(packet)
        wall_elapsed = time.perf_counter() - wall_start
        print(
            f"  encoded {n_frames} frames in {wall_elapsed:.2f}s "
            f"({n_frames / wall_elapsed:.1f} fps wall)"
        )
    finally:
        container.close()
    size_mb = MKV_PATH.stat().st_size / 1024 / 1024
    print(f"  MKV size: {size_mb:.2f} MB")


def remux_to_mp4() -> None:
    """Remux MKV -> MP4 via the bundled ffmpeg subprocess.

    PyAV's add_stream_from_template across MKV->MP4 doesn't apply the
    h264_mp4toannexb bitstream filter, so the resulting MP4 has SPS/PPS
    inline rather than in the avcC box. We hand this off to ffmpeg which
    handles it correctly. The remux is offline on a closed file so the
    subprocess/pipe failure modes that bit the live-recording path don't
    apply here.
    """
    import subprocess
    ffmpeg = Path(__file__).parent.parent / "resources" / "ffmpeg" / "ffmpeg.exe"
    print(f"Remuxing {MKV_PATH.name} -> {MP4_PATH.name} with +faststart (ffmpeg)")
    cmd = [
        str(ffmpeg),
        "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(MKV_PATH),
        "-c", "copy",
        "-movflags", "+faststart",
        str(MP4_PATH),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed: {r.stderr.strip()}")
    size_mb = MP4_PATH.stat().st_size / 1024 / 1024
    print(f"  MP4 size: {size_mb:.2f} MB")


def verify_mp4() -> None:
    print(f"Verifying {MP4_PATH.name}")
    inp = av.open(str(MP4_PATH))
    try:
        vs = inp.streams.video[0]
        print(f"  codec: {vs.codec_context.name} {vs.width}x{vs.height}")
        print(f"  duration: {float(inp.duration) / av.time_base:.2f}s")
        frame_count = sum(1 for _ in inp.decode(video=0))
        print(f"  decoded frames: {frame_count}")
    finally:
        inp.close()


def main() -> int:
    try:
        encode_mkv()
        remux_to_mp4()
        verify_mp4()
    except Exception as e:
        print(f"\nFAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("\nPASS — PyAV + h264_nvenc + MKV->MP4 remux works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
