"""Small H.264/AAC Momento fixtures created without the bundled helper tools."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av
import numpy as np


def make_momento_mkv(
    path: Path,
    *,
    duration_seconds: float = 3.0,
    fps: int = 30,
    width: int = 320,
    height: int = 240,
    with_audio: bool = True,
    game_slug: str = "fixture-game",
    live: bool = False,
) -> Path:
    """Create a deterministic recording-shaped fixture through PyAV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    sample_rate = 48_000
    frame_count = max(1, int(round(duration_seconds * fps)))
    sample_count = max(1, int(round(duration_seconds * sample_rate)))

    container = av.open(
        str(path),
        mode="w",
        format="matroska",
        options={"live": "1"} if live else None,
    )
    try:
        container.metadata["MOMENTO_GAME"] = game_slug
        video = container.add_stream("libx264", rate=fps)
        video.width = width
        video.height = height
        video.pix_fmt = "yuv420p"
        video.options = {"preset": "ultrafast", "crf": "30", "g": str(fps)}

        audio = None
        if with_audio:
            audio = container.add_stream("aac", rate=sample_rate)
            audio.layout = "stereo"
            audio.bit_rate = 96_000

        audio_cursor = 0
        for index in range(frame_count):
            image = np.zeros((height, width, 3), dtype=np.uint8)
            image[:, :, 0] = (index * 7) % 256
            image[:, :, 1] = np.arange(width, dtype=np.uint8)[None, :]
            image[:, :, 2] = np.arange(height, dtype=np.uint8)[:, None]
            image[:, (index * 5) % width : ((index * 5) % width) + 8] = 255
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in video.encode(frame):
                container.mux(packet)

            target_audio = min(sample_count, ((index + 1) * sample_rate) // fps)
            while audio is not None and audio_cursor < target_audio:
                samples = min(1024, target_audio - audio_cursor)
                positions = (np.arange(samples, dtype=np.float32) + audio_cursor) / sample_rate
                tone = (0.08 * np.sin(2 * np.pi * 440.0 * positions)).astype(np.float32)
                packed = np.stack((tone, tone), axis=1).reshape(1, -1)
                audio_frame = av.AudioFrame.from_ndarray(
                    packed,
                    format="flt",
                    layout="stereo",
                )
                audio_frame.sample_rate = sample_rate
                audio_frame.pts = audio_cursor
                audio_frame.time_base = Fraction(1, sample_rate)
                for packet in audio.encode(audio_frame):
                    container.mux(packet)
                audio_cursor += samples

        for packet in video.encode(None):
            container.mux(packet)
        if audio is not None:
            for packet in audio.encode(None):
                container.mux(packet)
    finally:
        container.close()
    return path
