"""Smoke test: audio PTS re-anchoring bounds A/V drift under dropped samples.

The encoder advances audio PTS by accumulated sample count. If the capture
thread is starved and WASAPI drops input samples, that count lags wallclock and
audio creeps ahead of video over a long recording. `_next_audio_pts` re-anchors
to wallclock when the count drifts too far behind. This test drives that helper
with a simulated drop burst and asserts the drift stays bounded — and that the
old pure-count behaviour would NOT have.

Run: .venv\\Scripts\\python.exe tests\\smoke_audio_resync.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import momento.core.encoder as encmod  # noqa: E402

SR = 48000
CHUNK = 960          # 20 ms at 48 kHz
CHUNK_S = CHUNK / SR
THRESH = encmod._AUDIO_RESYNC_THRESHOLD_S

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


def main() -> None:
    # Shell encoder with just the attributes _next_audio_pts touches.
    enc = encmod.InProcessEncoder.__new__(encmod.InProcessEncoder)
    enc._audio_offset_s = 0.0
    enc._mic_anchored = False
    enc._mic_pts_samples = 0

    wall = 0.0            # real elapsed time (what video is stamped against)
    naive_count = 0       # what the old pure-count behaviour would produce
    naive_anchored = False
    fixed_lag_max = 0.0
    naive_lag_max = 0.0
    last_pts = -1
    monotonic = True

    # Each chunk is a full CHUNK-frame read. A "drop" chunk = the capture thread
    # fell behind so 2x real time elapsed but only CHUNK frames were delivered
    # (WASAPI discarded the rest): 30 drops => 0.6 s of audio lost.
    plan = [("normal", 50), ("drop", 30), ("normal", 50)]
    for kind, n in plan:
        for _ in range(n):
            wall += CHUNK_S * (2 if kind == "drop" else 1)

            pts = enc._next_audio_pts("mic", wall, CHUNK, SR)
            if pts <= last_pts:
                monotonic = False
            last_pts = pts
            fixed_lag_max = max(fixed_lag_max, wall - pts / SR)

            if not naive_anchored:
                naive_count = int(wall * SR)
                naive_anchored = True
            naive_lag_max = max(naive_lag_max, wall - naive_count / SR)
            naive_count += CHUNK

    print(f"fixed max lag: {fixed_lag_max * 1000:6.1f} ms  (re-anchored)")
    print(f"naive max lag: {naive_lag_max * 1000:6.1f} ms  (old pure-count, for contrast)")
    print()

    check("audio PTS stays monotonic", monotonic)
    bound = THRESH + CHUNK_S
    check(
        f"re-anchor bounds drift to <= {bound * 1000:.0f} ms",
        fixed_lag_max <= bound + 1e-6,
    )
    check(
        "without the fix the drift would have been large (> 400 ms)",
        naive_lag_max > 0.4,
    )

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
