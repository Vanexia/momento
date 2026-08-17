"""Pure-logic checks for multichannel-to-stereo audio downmixing."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.audio_devices import to_channels  # noqa: E402


def main() -> int:
    passed = 0
    failed = 0

    def check(condition: bool, label: str) -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"PASS - {label}")
        else:
            failed += 1
            print(f"FAIL - {label}")

    center_only = np.zeros((4, 6), dtype=np.float32)
    center_only[:, 2] = 1.0
    center_mix = to_channels(center_only, 2)
    check(np.all(center_mix[:, 0] > 0), "5.1 center channel reaches stereo left")
    check(np.all(center_mix[:, 1] > 0), "5.1 center channel reaches stereo right")

    surround = np.zeros((4, 6), dtype=np.float32)
    surround[:, 4] = 1.0
    surround_mix = to_channels(surround, 2)
    check(np.all(surround_mix[:, 0] > 0), "5.1 left surround reaches stereo left")

    full_scale = to_channels(np.ones((4, 8), dtype=np.float32), 2)
    check(float(np.max(np.abs(full_scale))) <= 1.0, "7.1 downmix remains bounded")

    print(f"\n{passed}/{passed + failed} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
