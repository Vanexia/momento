"""Deterministic queue-pressure regressions; no capture or GPU work required."""

from __future__ import annotations

import logging
import queue
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from momento.core.encoder import InProcessEncoder  # noqa: E402


class _ConsumerDrainsAfterFull(queue.Queue):
    """Schedule a consumer drain between the producer's Full and retry."""

    def __init__(self) -> None:
        super().__init__(maxsize=8)
        self.consumed = []

    def put_nowait(self, item) -> None:
        try:
            super().put_nowait(item)
        except queue.Full:
            # The real queue has reported Full. Its consumer can run before
            # the producer catches that exception and attempts an eviction.
            while True:
                try:
                    self.consumed.append(self.get_nowait())
                except queue.Empty:
                    break
            raise


def _encoder() -> InProcessEncoder:
    encoder = InProcessEncoder(
        Path("unused.mkv"),
        video_width=8,
        video_height=6,
        video_framerate=30,
        video_codec="libx264",
        video_options={},
    )
    # Exercise submission and statistics without opening hardware or a file.
    encoder._started = True
    return encoder


class BackpressureTests(unittest.TestCase):
    def test_consumer_drain_after_full_does_not_report_frame_loss(self) -> None:
        encoder = _encoder()
        q = _ConsumerDrainsAfterFull()
        encoder._video_q = q
        warnings = []
        encoder.on_video_degraded = warnings.append
        frame = np.zeros((6, 8, 4), dtype=np.uint8)
        results = [encoder.submit_video(frame, i / 30) for i in range(300)]
        while not q.empty():
            q.consumed.append(q.get_nowait())

        self.assertEqual([item.pts for item in q.consumed], list(range(300)))
        self.assertEqual(encoder._stats.video_frames_submitted, 300)
        self.assertEqual(encoder._stats.video_frames_dropped, 0)
        self.assertTrue(all(results))
        self.assertFalse(encoder._stats.video_health_degraded)
        self.assertEqual(warnings, [])

    def test_full_queue_still_reports_eviction_and_keeps_latest_frames(self) -> None:
        encoder = _encoder()
        frame = np.zeros((6, 8, 4), dtype=np.uint8)
        results = [encoder.submit_video(frame, i / 30) for i in range(9)]
        remaining = [encoder._video_q.get_nowait().pts for _ in range(8)]

        self.assertEqual(results, [True] * 8 + [False])
        self.assertEqual(remaining, list(range(1, 9)))
        self.assertEqual(encoder._stats.video_frames_submitted, 9)
        self.assertEqual(encoder._stats.video_frames_dropped, 1)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
