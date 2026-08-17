"""Regression test for the highlight pager's prev/next navigation.

Bug: when the playhead was a few seconds past highlight 2 ("2 / 2"), one click
of the prev arrow jumped to the marker just before the playhead — which was
highlight 2 itself — so it restarted the current highlight and needed a SECOND
click to reach highlight 1. The fix navigates by index, so prev/next always
step exactly one highlight.

Constructs a real VideoPreview (QMediaPlayer/QVideoWidget) — needs the real
platform plugin, not offscreen.

    python tests/smoke_highlight_pager.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from momento.ui.preview import VideoPreview

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}", flush=True)
    else:
        _FAIL += 1
        print(f"  FAIL {name}", flush=True)


def main() -> int:
    # Held in a local for the duration so Qt isn't torn down mid-test.
    app = QApplication.instance() or QApplication([])  # noqa: F841
    pv = VideoPreview()
    pv.set_highlights([8341.0, 8726.0])  # two highlights (bookmark times)

    # Drive position + seek deterministically (no real async playback).
    state = {"pos": 8730.0}  # a few seconds PAST highlight 2
    pv.position = lambda: state["pos"]
    seeks: list[float] = []

    def fake_seek(s: float) -> None:
        seeks.append(float(s))
        state["pos"] = float(s)
    pv.seek = fake_seek

    check("playhead reads as highlight 2 (index 1)", pv._current_highlight_index() == 1)

    seeks.clear()
    pv._goto_prev_highlight()
    check("ONE prev click from 2/2 -> highlight 1 (not a restart)",
          len(seeks) == 1 and abs(seeks[0] - 8341.0) < 0.1)
    check("now on highlight 1 (index 0)", pv._current_highlight_index() == 0)

    seeks.clear()
    pv._goto_next_highlight()
    check("next -> highlight 2", len(seeks) == 1 and abs(seeks[0] - 8726.0) < 0.1)

    state["pos"] = 8341.0
    seeks.clear()
    pv._goto_prev_highlight()
    check("prev at the first highlight clamps to first (no underflow)",
          len(seeks) == 1 and abs(seeks[0] - 8341.0) < 0.1)

    state["pos"] = 8726.0
    seeks.clear()
    pv._goto_next_highlight()
    check("next at the last highlight clamps to last (no overflow)",
          len(seeks) == 1 and abs(seeks[0] - 8726.0) < 0.1)

    print(f"\n{_PASS} passed, {_FAIL} failed", flush=True)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
