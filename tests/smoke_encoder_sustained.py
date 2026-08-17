"""Manual 1440p60 queue-pressure test for the in-process encoder.

This is intentionally excluded from hosted CI because it requires a real GPU.
It approximates WGC's per-frame owned-memory copy and runs long enough to cross
the encoder's sustained-drop health window.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core import encoders  # noqa: E402
from momento.core.encoder import InProcessEncoder  # noqa: E402


WIDTH = 2560
HEIGHT = 1440
FPS = 60
DURATION_S = 10
SAMPLE_RATE = 48_000
CHUNK_FRAMES = 960


def main() -> int:
    codec = encoders.pick_encoder()
    options = encoders.quality_options_for(codec, "high", 12_000)
    pix_fmt = encoders.preferred_pix_fmt_for(codec)
    template = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    template[:, :, 3] = 255
    silence = np.zeros((CHUNK_FRAMES, 2), dtype=np.float32)

    with tempfile.TemporaryDirectory(prefix="momento_1440p_") as folder:
        output = Path(folder) / "sustained.mkv"
        encoder = InProcessEncoder(
            output,
            video_width=WIDTH,
            video_height=HEIGHT,
            video_framerate=FPS,
            video_codec=codec,
            video_options=options,
            encoder_pix_fmt=pix_fmt,
        )
        encoder.start()
        start = time.perf_counter()
        audio_index = 0
        for frame_index in range(FPS * DURATION_S):
            timestamp = frame_index / FPS
            delay = start + timestamp - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            frame = template.copy()
            bar_x = (frame_index * 13) % WIDTH
            frame[:, bar_x : min(WIDTH, bar_x + 16), :3] = 255
            encoder.submit_video(frame, pts_seconds=timestamp)
            while audio_index * CHUNK_FRAMES / SAMPLE_RATE <= timestamp:
                audio_time = audio_index * CHUNK_FRAMES / SAMPLE_RATE
                encoder.submit_mic_audio(silence, pts_seconds=audio_time)
                encoder.submit_sys_audio(silence, pts_seconds=audio_time)
                audio_index += 1

        stats = encoder.stop()
        with av.open(str(output)) as container:
            decoded = sum(1 for _ in container.decode(video=0))
        with av.open(str(output)) as container:
            stream = container.streams.video[0]
            dts = [packet.dts for packet in container.demux(stream) if packet.dts is not None]
            dts_is_strict = all(current > previous for previous, current in zip(dts, dts[1:]))

    print(f"encoder: {encoders.display_name_for(codec)} ({pix_fmt})")
    print(stats.summary())
    print(f"decoded video frames: {decoded}")
    print(f"strictly increasing video DTS: {dts_is_strict} ({len(dts)} packets)")
    healthy = (
        encoder.fatal_error is None
        and stats.video_drop_rate <= 0.01
        and decoded == stats.video_frames_encoded
        and decoded >= FPS * DURATION_S * 0.99
        and dts_is_strict
    )
    print(f"{'PASS' if healthy else 'FAIL'} - sustained 1440p60 encode")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
