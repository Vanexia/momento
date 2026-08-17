"""Regression: a stale first-frame timer must not pause a newer source."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.ui.preview import VideoPreview  # noqa: E402


class _FakePlayer:
    def __init__(self) -> None:
        self.paused = 0
        self.positions: list[int] = []

    def position(self) -> int:
        return 50

    def pause(self) -> None:
        self.paused += 1

    def setPosition(self, value: int) -> None:
        self.positions.append(value)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    preview = VideoPreview()
    player = _FakePlayer()
    preview._player = player
    preview._source_generation = 2

    try:
        preview._prime_pause(1)
    except TypeError:
        print("FAIL - priming callback is not source-generation aware")
        return 1

    stale_ignored = player.paused == 0 and not player.positions
    print(f"{'PASS' if stale_ignored else 'FAIL'} - stale prime timer ignored")

    preview._prime_pause(2)
    current_applied = player.paused == 1 and player.positions == [0]
    print(f"{'PASS' if current_applied else 'FAIL'} - current prime timer pauses first frame")
    preview.deleteLater()
    app.processEvents()
    return 0 if stale_ignored and current_applied else 1


if __name__ == "__main__":
    raise SystemExit(main())
