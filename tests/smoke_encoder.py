"""Smoke test for momento.core.encoder.InProcessEncoder.

Feeds synthetic BGRA video frames + two synthetic audio streams (mic = 440Hz
sine, system = 220Hz sine) into the encoder for 3 seconds, then verifies the
resulting MKV decodes cleanly with the expected frame count and audio
duration.

Usage:
    .venv\\Scripts\\python.exe tests\\smoke_encoder.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from momento.core.encoder import InProcessEncoder
from momento.core import encoders

WIDTH = 1280
HEIGHT = 720
FPS = 60
DURATION_S = 3
SAMPLE_RATE = 48000
CHUNK_FRAMES = 960  # ~20 ms

OUT_PATH = Path(__file__).parent.parent / "recordings" / "smoke_encoder.mkv"


def synth_video_frame(t: float) -> np.ndarray:
    """BGRA frame with a moving vertical bar so visual changes are obvious."""
    img = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    img[:, :, 3] = 255  # alpha
    bar_x = int((t * 200) % WIDTH)
    img[:, max(0, bar_x - 10): bar_x + 10, :3] = 255
    return img


def synth_audio(freq_hz: float, n_frames: int, sr: int, phase: float) -> tuple[np.ndarray, float]:
    """Generate `n_frames` of stereo float32 sine at `freq_hz`. Returns (samples, next_phase)."""
    t = np.arange(n_frames, dtype=np.float32) / sr
    s = 0.2 * np.sin(2 * np.pi * freq_hz * t + phase).astype(np.float32)
    next_phase = (phase + 2 * np.pi * freq_hz * n_frames / sr) % (2 * np.pi)
    stereo = np.stack([s, s], axis=1)
    return stereo, next_phase


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if OUT_PATH.exists():
        OUT_PATH.unlink()

    # Phase 12+: pick whichever encoder the host actually supports so
    # the smoke test isn't locked to NVIDIA. On a CI box with no GPU
    # this lands on libx264; on a dev machine with NVENC it lands on
    # NVENC. quality_options_for + preferred_pix_fmt_for return the
    # correct per-backend wiring for the chosen encoder.
    video_codec = encoders.pick_encoder()
    video_options = encoders.quality_options_for(video_codec, "high", 12000)
    encoder_pix_fmt = encoders.preferred_pix_fmt_for(video_codec)
    print(f"smoke encoder: {encoders.display_name_for(video_codec)} (pix_fmt={encoder_pix_fmt})")

    enc = InProcessEncoder(
        OUT_PATH,
        video_width=WIDTH,
        video_height=HEIGHT,
        video_framerate=FPS,
        video_pix_fmt="bgra",
        mic_sample_rate=SAMPLE_RATE,
        mic_channels=2,
        sys_sample_rate=SAMPLE_RATE,
        sys_channels=2,
        mic_volume=1.0,
        sys_volume=1.0,
        video_codec=video_codec,
        video_options=video_options,
        encoder_pix_fmt=encoder_pix_fmt,
    )
    enc.start()

    n_video = DURATION_S * FPS
    n_audio_chunks = (DURATION_S * SAMPLE_RATE) // CHUNK_FRAMES

    mic_phase = 0.0
    sys_phase = 0.0
    wall_start = time.perf_counter()
    # Wallclock-paced submission so the test simulates a real capture
    # pipeline at 60 fps rather than firehosing the encoder.
    audio_idx = 0
    for i in range(n_video):
        t = i / FPS
        # Wait until wallclock catches up to the next frame's stamp.
        deadline = wall_start + t
        delay = deadline - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        enc.submit_video(synth_video_frame(t), pts_seconds=t)
        while audio_idx / (SAMPLE_RATE / CHUNK_FRAMES) <= t and audio_idx < n_audio_chunks:
            mic_samples, mic_phase = synth_audio(440.0, CHUNK_FRAMES, SAMPLE_RATE, mic_phase)
            sys_samples, sys_phase = synth_audio(220.0, CHUNK_FRAMES, SAMPLE_RATE, sys_phase)
            chunk_t = audio_idx * CHUNK_FRAMES / SAMPLE_RATE
            enc.submit_mic_audio(mic_samples, pts_seconds=chunk_t)
            enc.submit_sys_audio(sys_samples, pts_seconds=chunk_t)
            audio_idx += 1

    while audio_idx < n_audio_chunks:
        mic_samples, mic_phase = synth_audio(440.0, CHUNK_FRAMES, SAMPLE_RATE, mic_phase)
        sys_samples, sys_phase = synth_audio(220.0, CHUNK_FRAMES, SAMPLE_RATE, sys_phase)
        chunk_t = audio_idx * CHUNK_FRAMES / SAMPLE_RATE
        enc.submit_mic_audio(mic_samples, pts_seconds=chunk_t)
        enc.submit_sys_audio(sys_samples, pts_seconds=chunk_t)
        audio_idx += 1

    stats = enc.stop()
    wall_elapsed = time.perf_counter() - wall_start
    print(f"submitted in {wall_elapsed:.2f}s")
    print(stats.summary())

    if enc.fatal_error is not None:
        print(f"FATAL ERROR in worker: {enc.fatal_error!r}", file=sys.stderr)
        return 2

    # Verify by decoding the MKV.
    print(f"\nVerifying {OUT_PATH}")
    inp = av.open(str(OUT_PATH))
    try:
        vs = inp.streams.video[0]
        ass = inp.streams.audio[0]
        print(f"  video: {vs.codec_context.name} {vs.width}x{vs.height}")
        print(f"  audio: {ass.codec_context.name} {ass.sample_rate}Hz "
              f"{ass.codec_context.layout.name}")
        v_count = sum(1 for _ in inp.decode(video=0))
        inp.seek(0)
        a_samples = sum(f.samples for f in inp.decode(audio=0))
        a_seconds = a_samples / ass.sample_rate
        print(f"  decoded video frames: {v_count}  (expected ~{n_video})")
        print(f"  decoded audio seconds: {a_seconds:.2f}  (expected ~{DURATION_S})")
    finally:
        inp.close()

    if v_count < n_video * 0.9:
        print("FAIL: too few video frames", file=sys.stderr)
        return 3
    if a_seconds < DURATION_S * 0.9:
        print("FAIL: too little audio", file=sys.stderr)
        return 4
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
