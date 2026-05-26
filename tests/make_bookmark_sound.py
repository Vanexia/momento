"""Generate the bookmark notification chime.

Writes ``resources/sounds/bookmark.wav`` — a soft two-note ascending chime
(A5 → E6) with quick attack and exponential decay. ~200 ms total, peak
amplitude 0.18 so it's audible but never harsh. The actual playback
volume in-app is further attenuated via QSoundEffect.setVolume().

Run once when the sound needs to change; checked in alongside the source.

    .venv\\Scripts\\python.exe tests\\make_bookmark_sound.py
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "resources" / "sounds" / "bookmark.wav"
SAMPLE_RATE = 48000
AMP = 0.18


def render() -> np.ndarray:
    """Return a (frames, 2) int16 numpy array containing the chime."""
    # Note 1: A5 = 880 Hz, 90 ms
    # Note 2: E6 = 1318.5 Hz, 130 ms, starting 60 ms in (slight overlap)
    total_ms = 200
    n = int(SAMPLE_RATE * total_ms / 1000)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE

    def note(freq: float, start_s: float, dur_s: float) -> np.ndarray:
        local_t = t - start_s
        # Mask outside the note window.
        mask = (local_t >= 0) & (local_t < dur_s)
        # 10 ms linear attack, then exponential decay. Sounds soft, not clicky.
        attack = 0.010
        env = np.where(
            local_t < attack,
            local_t / attack,
            np.exp(-(local_t - attack) * 14.0),
        )
        env = np.where(mask, env, 0.0).astype(np.float32)
        # A small bit of even-harmonic to round the timbre off the pure sine.
        wave_ = (
            np.sin(2 * np.pi * freq * local_t)
            + 0.10 * np.sin(2 * np.pi * 2 * freq * local_t)
        )
        return (wave_ * env).astype(np.float32)

    mono = note(880.0, 0.000, 0.090) + note(1318.5, 0.060, 0.130)
    # Normalise then scale to amp.
    peak = float(np.max(np.abs(mono)))
    if peak > 0:
        mono = mono / peak * AMP
    # Soft 5 ms fade-out at the very end to avoid a click on file termination.
    fade_n = int(SAMPLE_RATE * 0.005)
    mono[-fade_n:] *= np.linspace(1.0, 0.0, fade_n, dtype=np.float32)

    stereo = np.stack([mono, mono], axis=1)
    return (stereo * 32767.0).astype(np.int16)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    samples = render()
    with wave.open(str(OUT), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes(order="C"))
    print(f"Wrote {OUT}  ({OUT.stat().st_size:,} bytes, "
          f"{samples.shape[0] / SAMPLE_RATE * 1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
