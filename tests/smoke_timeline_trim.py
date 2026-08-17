"""Regression test: the timeline must not wipe the user's trim (2026-07-02).

The bug: ``Timeline.set_duration`` reset the trim handles, zoom and playhead
on EVERY call — and it is wired to the preview's ``duration_changed``, which
QMediaPlayer's WMF backend re-emits freely (playback start, pause, duration
refinement, plus the ffprobe hint landing). Net effect: drag the end handle to
shorten a clip, hit "Play clip portion", and the end handle teleports back to
the full duration — the user literally could not trim. The same reset firing
between a drag and Export would silently export the FULL recording.

Now only a fresh load (duration 0 -> D, which the editor triggers explicitly
on selection change) resets state; a same-clip duration re-report preserves
the handles / zoom / playhead, clamped to the new duration.

Headless-safe (offscreen QApplication). Run:
.venv\\Scripts\\python.exe tests\\smoke_timeline_trim.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.ui.timeline import Timeline  # noqa: E402

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def main() -> None:
    _app = QApplication.instance() or QApplication([])

    t = Timeline()

    # Fresh load: 0 -> D resets everything to the full clip.
    t.set_duration(0.0)
    t.set_duration(100.0)
    check("fresh load: handles span full clip",
          close(t.start_seconds, 0.0) and close(t.end_seconds, 100.0))
    check("fresh load: view spans full clip",
          close(t.view_start, 0.0) and close(t.view_end, 100.0))

    # User trims (public API equivalent of dragging both handles).
    t.set_clip_range(10.0, 50.0)
    check("trim applied", close(t.start_seconds, 10.0) and close(t.end_seconds, 50.0))

    # THE bug: a same-value duration re-emission (WMF does this at playback
    # start — i.e. the moment the user hits "Play clip portion").
    t.set_duration(100.0)
    check("same-value re-emit: trim preserved",
          close(t.start_seconds, 10.0) and close(t.end_seconds, 50.0))

    # Duration refinement (slightly different value) mid-clip.
    t.set_duration(100.4)
    check("refinement: trim preserved",
          close(t.start_seconds, 10.0) and close(t.end_seconds, 50.0))
    check("refinement: full view follows new duration",
          close(t.view_start, 0.0) and close(t.view_end, 100.4))

    # An untrimmed end (== old duration) follows the refinement.
    t.set_duration(0.0)
    t.set_duration(100.0)
    t.set_duration(102.0)
    check("untrimmed end follows a growing duration", close(t.end_seconds, 102.0))

    # Duration shrinking below the user's end clamps the trim.
    t.set_duration(0.0)
    t.set_duration(100.0)
    t.set_clip_range(10.0, 99.0)
    t.set_duration(95.0)
    check("shrink: end clamped to new duration",
          close(t.end_seconds, 95.0) and close(t.start_seconds, 10.0))

    # A zoomed view survives a same-clip refinement.
    t.set_duration(0.0)
    t.set_duration(100.0)
    t.set_view(20.0, 30.0)
    t.set_clip_range(22.0, 28.0)
    t.set_duration(100.2)
    check("zoomed view preserved across refinement",
          close(t.view_start, 20.0) and close(t.view_end, 30.0))
    check("trim preserved across refinement while zoomed",
          close(t.start_seconds, 22.0) and close(t.end_seconds, 28.0))

    # Playhead clamps to a shrunk duration but is otherwise untouched.
    t.set_duration(0.0)
    t.set_duration(100.0)
    t.set_playhead(80.0)
    t.set_duration(70.0)
    check("playhead clamped to shrunk duration", close(70.0, t._playhead))

    # Selection change (explicit 0) then a new clip still fully resets —
    # including when the new clip happens to have the SAME duration.
    t.set_duration(0.0)
    check("selection cleared: duration 0", close(t.duration, 0.0))
    t.set_duration(70.0)
    check("new clip with same duration as last: full reset",
          close(t.start_seconds, 0.0) and close(t.end_seconds, 70.0)
          and close(t.view_start, 0.0) and close(t.view_end, 70.0))

    # Overlapping handles ("Set start here" near the end / minimum-length
    # selection): the press side must disambiguate, or the end handle is
    # un-grabbable (the old hit test always answered 'start').
    t.resize(800, 80)
    t.set_duration(0.0)
    t.set_duration(100.0)
    t.set_clip_range(99.0, 100.0)  # ~8 px apart at this width — both in slop
    sx = t._seconds_to_x(t.start_seconds)
    ex = t._seconds_to_x(t.end_seconds)
    check("overlap: press left of the pair grabs start", t._handle_at(sx - 4) == "start")
    check("overlap: press right of the pair grabs end", t._handle_at(ex + 4) == "end")
    t.set_clip_range(10.0, 50.0)
    check("no overlap: end handle still answers end",
          t._handle_at(t._seconds_to_x(50.0)) == "end")

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
