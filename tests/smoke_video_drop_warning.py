"""The frame-loss warning must reflect sustained loss, not a single hitch.

Field logs showed the old 300-frame (5 s) window warning on recordings whose
total loss was 0.005%: one quarter-second stall drops ~15 frames from the
8-frame queue, which read as "5% of recent frames". These cases pin the
one-minute, frame-rate-scaled window and the start-up grace period.
"""

from __future__ import annotations

import logging
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from momento.core.encoder import InProcessEncoder  # noqa: E402


def _encoder(fps: int = 60) -> tuple[InProcessEncoder, list[float]]:
    encoder = InProcessEncoder(
        Path("unused.mkv"),
        video_width=8,
        video_height=6,
        video_framerate=fps,
        video_codec="libx264",
        video_options={},
    )
    warnings: list[float] = []
    encoder.on_video_degraded = warnings.append
    return encoder, warnings


def _feed(encoder: InProcessEncoder, outcomes) -> None:
    for dropped in outcomes:
        encoder._record_video_frame_outcome(dropped=dropped)


class VideoDropWarningTests(unittest.TestCase):
    def test_window_is_one_minute_at_the_recording_frame_rate(self) -> None:
        self.assertEqual(_encoder(60)[0]._video_drop_window.maxlen, 3600)
        self.assertEqual(_encoder(120)[0]._video_drop_window.maxlen, 7200)
        self.assertEqual(_encoder(30)[0]._video_drop_window.maxlen, 1800)

    def test_field_log_hitch_no_longer_warns(self) -> None:
        # The exact field pattern: 15 frames lost inside 300, then clean.
        encoder, warnings = _encoder(60)
        _feed(encoder, [False] * 1000 + [True] * 15 + [False] * 285 + [False] * 5000)
        self.assertEqual(warnings, [])

    def test_repeated_brief_hitches_stay_quiet(self) -> None:
        # Four ~0.6 s hitches in a minute is ~4% — noticeable stutter, but not
        # the sustained degradation the warning's advice is meant for.
        encoder, warnings = _encoder(60)
        minute = ([True] * 36 + [False] * 864) * 4
        _feed(encoder, minute * 3)
        self.assertEqual(warnings, [])

    def test_sustained_loss_warns_once_with_the_rolling_rate(self) -> None:
        encoder, warnings = _encoder(60)
        _feed(encoder, ([True] + [False] * 9) * 1200)  # 10% for 3+ minutes
        self.assertEqual(len(warnings), 1)
        self.assertAlmostEqual(warnings[0], 0.10, places=3)

    def test_window_count_matches_the_window_contents(self) -> None:
        encoder, _ = _encoder(30)
        _feed(encoder, ([True] * 3 + [False] * 4) * 2000)
        window = encoder._video_drop_window
        self.assertEqual(encoder._video_drop_window_dropped, sum(window))

    def test_start_up_grace_period_ignores_load_in_bursts(self) -> None:
        encoder, warnings = _encoder(60)
        encoder._started = True
        frame = np.zeros((6, 8, 4), dtype=np.uint8)
        # Recording just started: a full queue drops every frame for 30 s.
        encoder._t0_monotonic = time.monotonic()
        for i in range(1800):
            encoder.submit_video(frame, i / 60)
        self.assertGreater(encoder._stats.video_frames_dropped, 1700)
        self.assertEqual(len(encoder._video_drop_window), 0)
        self.assertEqual(warnings, [])
        # After the grace period, frame outcomes are judged again.
        encoder._t0_monotonic = time.monotonic() - 61.0
        encoder.submit_video(frame, 1800 / 60)
        self.assertEqual(len(encoder._video_drop_window), 1)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
