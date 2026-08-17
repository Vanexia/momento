"""Regression: the encoder's monotonic-PTS guard + audio-leg stall watchdog.

Covers the two encoder-audio fixes from the 2026-07-04 audit:

  1. MONOTONIC-PTS GUARD — amix can emit a mixed frame whose PTS steps BACKWARD
     (the mic + system legs anchor to wallclock independently and loopback can
     come online late, so amix re-mixes an already-emitted region). Feeding that
     backward timestamp to the Matroska muxer raises av EINVAL, which used to
     propagate out of _audio_worker, kill the whole audio worker, drop ALL
     further audio, and fail finalize — the long-"unexplained" encoder.py:683 /
     "returned 22" incident. _encode_and_mux_audio must DROP such frames.

  2. STALL WATCHDOG — with amix(duration=longest), a leg that stays open but
     stops delivering (a mic whose driver wedges without going inactive) makes
     amix emit NOTHING and buffer the flowing leg without bound. _check_audio_stall
     must EOF the starved leg so mixing continues (degrade to one leg).

Uses only libav (aac + matroska) — no GPU / audio devices — so it runs on CI.
"""

import sys
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.encoder import (  # noqa: E402
    InProcessEncoder,
    _AUDIO_STALL_TIMEOUT_S,
)

_passed = 0
_failed = 0


def check(cond: bool, label: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS - {label}")
    else:
        _failed += 1
        print(f"FAIL - {label}")


def _make_encoder(out_path: Path) -> InProcessEncoder:
    # Construction only opens nothing (start() builds streams/threads); the
    # video codec name is never used here, so it needn't be available.
    return InProcessEncoder(
        out_path,
        video_width=64,
        video_height=64,
        video_framerate=30,
        video_codec="libx264",
        video_options={},
    )


def _make_frame(pts: int, nsamples: int = 1024) -> av.AudioFrame:
    data = np.zeros((2, nsamples), dtype=np.float32)  # fltp: (channels, samples)
    f = av.AudioFrame.from_ndarray(data, format="fltp", layout="stereo")
    f.sample_rate = 48000
    f.pts = pts
    f.time_base = Fraction(1, 48000)
    return f


def test_monotonic_pts_guard(tmp: Path) -> None:
    out = tmp / "guard.mkv"
    enc = _make_encoder(out)
    container = av.open(str(out), mode="w", format="matroska")
    ass = container.add_stream("aac", rate=48000)
    ass.bit_rate = 128_000
    enc._container = container
    enc._audio_stream = ass

    # Four monotonically-increasing frames — all accepted, none dropped.
    for i in range(4):
        enc._encode_and_mux_audio(_make_frame(i * 1024), ass)
    check(enc._audio_pts_dropped == 0, "monotonic frames: none dropped")

    # A frame whose PTS steps BACKWARD (1500 < the last accepted 3072). Without
    # the guard this reaches container.mux() and raises EINVAL, crashing the
    # worker. It must be dropped instead — and NOT raise.
    raised = False
    try:
        enc._encode_and_mux_audio(_make_frame(1500), ass)
    except Exception as e:  # noqa: BLE001
        raised = True
        print(f"       (unexpected raise: {e})")
    check(not raised, "backward-PTS frame: does not raise")
    check(enc._audio_pts_dropped == 1, "backward-PTS frame: dropped")

    # A later forward frame resumes normally.
    enc._encode_and_mux_audio(_make_frame(4096), ass)
    check(enc._audio_pts_dropped == 1, "forward frame after drop: accepted")

    # Finalise and confirm a valid file with an audio stream was produced.
    for pkt in ass.encode(None):
        container.mux(pkt)
    container.close()
    probe = av.open(str(out))
    has_audio = any(s.type == "audio" for s in probe.streams)
    probe.close()
    check(has_audio, "guarded stream still finalises with audio")


def test_stall_watchdog(tmp: Path) -> None:
    stale = _AUDIO_STALL_TIMEOUT_S + 1.0

    # Both legs fresh -> nothing closed.
    enc = _make_encoder(tmp / "s1.mkv")
    enc._build_audio_graph()
    now = time.monotonic()
    enc._mic_last_data_wall = now
    enc._sys_last_data_wall = now
    enc._check_audio_stall()
    check(
        not enc._mic_input_closed and not enc._sys_input_closed,
        "both legs flowing: neither closed",
    )

    # Mic stale while system flows -> mic leg EOF'd, system left open.
    enc = _make_encoder(tmp / "s2.mkv")
    enc._build_audio_graph()
    enc._mic_last_data_wall = time.monotonic() - stale
    enc._sys_last_data_wall = time.monotonic()
    enc._check_audio_stall()
    check(
        enc._mic_input_closed and not enc._sys_input_closed,
        "mic stalled, system flowing: mic leg closed only",
    )

    # System stale while mic flows -> system leg EOF'd, mic left open.
    enc = _make_encoder(tmp / "s3.mkv")
    enc._build_audio_graph()
    enc._mic_last_data_wall = time.monotonic()
    enc._sys_last_data_wall = time.monotonic() - stale
    enc._check_audio_stall()
    check(
        enc._sys_input_closed and not enc._mic_input_closed,
        "system stalled, mic flowing: system leg closed only",
    )

    # Only one leg open -> single-leg stall is a no-op (nothing to fall back to).
    enc = _make_encoder(tmp / "s4.mkv")
    enc._build_audio_graph()
    enc._sys_input_closed = True  # system already gone
    enc._mic_last_data_wall = time.monotonic() - stale
    enc._check_audio_stall()
    check(not enc._mic_input_closed, "single open leg stalled: not force-closed")


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_monotonic_pts_guard(tmp)
        test_stall_watchdog(tmp)
    print(f"\n{_passed}/{_passed + _failed} checks passed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
