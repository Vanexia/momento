"""Each audio leg must land at its own timestamps in the recorded file.

Runs the encoder's real mix graph and AAC/Matroska output (software only, no
devices), then decodes the file and measures where marker clicks land.

Before the fix, amix took every output timestamp from the mic leg and stepped
system audio along sample-for-sample: game audio was placed late by the
mic-open latency, carried no timestamps at all with no mic (so it started at
0 regardless of when it began), and capture gaps were never filled.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av  # noqa: E402
import numpy as np  # noqa: E402

from momento.core.encoder import InProcessEncoder  # noqa: E402

SR = 48000
CHUNK = 960  # 20 ms, as the capture threads deliver
# AAC framing/priming can shift decoded samples by about one frame.
TOLERANCE_S = 0.03


def _chunk(pts: int, click: bool = False) -> av.AudioFrame:
    data = np.zeros((CHUNK, 2), dtype=np.float32)
    if click:
        data[0, :] = 0.9
    frame = av.AudioFrame.from_ndarray(data.reshape(1, -1), format="flt", layout="stereo")
    frame.sample_rate = SR
    frame.pts = pts
    frame.time_base = Fraction(1, SR)
    return frame


def _record(path: Path, steps) -> list[float]:
    """Push (leg, frame-or-None) steps through the encoder's graph into an MKV
    and return the decoded time of every click."""
    encoder = InProcessEncoder(
        path,
        video_width=8,
        video_height=6,
        video_framerate=30,
        video_codec="libx264",
        video_options={},
    )
    encoder._build_audio_graph()
    container = av.open(str(path), mode="w", format="matroska")
    stream = container.add_stream("aac", rate=SR)
    stream.bit_rate = 128_000
    encoder._container = container
    encoder._audio_stream = stream
    buffers = {"mic": encoder._audio_buf_mic, "sys": encoder._audio_buf_sys}
    for leg, frame in steps:
        buffers[leg].push(frame)
        encoder._drain_audio_sink(stream)
    for buf in buffers.values():
        buf.push(None)
    encoder._drain_audio_sink(stream)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()

    clicks: list[float] = []
    with av.open(str(path)) as probe:
        for frame in probe.decode(audio=0):
            samples = frame.to_ndarray()
            left = samples[0] if samples.ndim == 2 else samples
            for index in np.flatnonzero(np.abs(left) > 0.3):
                t = float(frame.time) + int(index) / SR
                if not clicks or t - clicks[-1] > 0.1:
                    clicks.append(t)
    return clicks


def _graph_click_positions(steps) -> list[int]:
    """Push steps through the encoder's real mix graph (no AAC) and return
    the exact output sample index of each click."""
    encoder = InProcessEncoder(
        Path("unused.mkv"),
        video_width=8,
        video_height=6,
        video_framerate=30,
        video_codec="libx264",
        video_options={},
    )
    encoder._build_audio_graph()
    buffers = {"mic": encoder._audio_buf_mic, "sys": encoder._audio_buf_sys}
    frames = []

    def drain() -> None:
        while True:
            try:
                frames.append(encoder._audio_sink.pull())
            except (av.error.BlockingIOError, av.error.EOFError):
                return

    for leg, frame in steps:
        buffers[leg].push(frame)
        drain()
    for buf in buffers.values():
        buf.push(None)
    drain()
    positions, cursor = [], 0
    for frame in frames:
        base = frame.pts if frame.pts is not None else cursor
        planar = frame.to_ndarray()  # aformat output is fltp: (channels, samples)
        positions.extend(base + int(i) for i in np.flatnonzero(np.abs(planar[0]) > 0.5))
        cursor = base + frame.samples
    return positions


class AudioTimelineTests(unittest.TestCase):
    def test_small_start_offsets_are_sample_exact(self) -> None:
        # Real-world loopback/mic open gaps are tens of milliseconds; resample
        # padding must place them exactly, below any compensation threshold.
        for sys_start, mic_start in ((0.05, 0.0), (0.02, 0.06), (0.08, 0.03), (0.25, 0.30)):
            with self.subTest(sys=sys_start, mic=mic_start):
                steps = []
                sys_pts, mic_pts = round(sys_start * SR), round(mic_start * SR)
                for i in range(100):
                    steps.append(("sys", _chunk(sys_pts, click=(i == 0))))
                    steps.append(("mic", _chunk(mic_pts)))
                    sys_pts += CHUNK
                    mic_pts += CHUNK
                self.assertEqual(_graph_click_positions(steps), [round(sys_start * SR)])

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="mementos_audio_timeline_")
        self.folder = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def assertClicks(self, clicks, expected) -> None:
        self.assertEqual(len(clicks), len(expected), f"clicks={clicks}")
        for got, want in zip(clicks, expected):
            self.assertLess(abs(got - want), TOLERANCE_S, f"clicks={clicks} expected={expected}")

    def test_game_audio_is_not_delayed_by_a_late_mic(self) -> None:
        # Loopback opens first (click at 0.0 s); the mic only starts at 0.5 s.
        steps = [("sys", _chunk(i * CHUNK, click=(i == 0))) for i in range(25)]
        mic_pts, sys_pts = SR // 2, 25 * CHUNK
        for _ in range(100):
            steps += [("mic", _chunk(mic_pts)), ("sys", _chunk(sys_pts))]
            mic_pts += CHUNK
            sys_pts += CHUNK
        self.assertClicks(_record(self.folder / "late_mic.mkv", steps), [0.0])

    def test_game_audio_keeps_its_time_without_a_mic(self) -> None:
        # No mic configured: its leg is closed before any data. System audio
        # began 2 s into the recording, so its click must play at 2 s, not 0.
        steps = [("mic", None)]
        steps += [("sys", _chunk(2 * SR + i * CHUNK, click=(i == 0))) for i in range(100)]
        self.assertClicks(_record(self.folder / "no_mic.mkv", steps), [2.0])

    def test_each_leg_keeps_its_own_start_time(self) -> None:
        steps = []
        mic_pts, sys_pts = SR, int(1.2 * SR)
        for i in range(150):
            steps.append(("mic", _chunk(mic_pts, click=(i == 0))))
            steps.append(("sys", _chunk(sys_pts, click=(i == 15))))
            mic_pts += CHUNK
            sys_pts += CHUNK
        self.assertClicks(
            _record(self.folder / "both_late.mkv", steps),
            [1.0, 1.2 + 15 * CHUNK / SR],
        )

    def test_capture_gap_is_filled_not_collapsed(self) -> None:
        # System audio loses a second of capture; audio after the gap must
        # stay at its real time instead of being pulled a second early.
        steps = []
        mic_pts = sys_pts = 0
        for i in range(200):
            steps.append(("mic", _chunk(mic_pts)))
            mic_pts += CHUNK
            if i == 50:
                sys_pts += SR
            steps.append(("sys", _chunk(sys_pts, click=(i == 50))))
            sys_pts += CHUNK
        self.assertClicks(
            _record(self.folder / "gap.mkv", steps),
            [50 * CHUNK / SR + 1.0],
        )


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
