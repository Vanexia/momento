"""Verify the thumbnail duration badge survives a list REBUILD.

Regression: the tray prebuilds the editor hidden (probes run, durations cache),
then calls refresh() when the window is opened. That rebuild re-adds every row
with duration=None and the metadata probe is skipped for already-probed paths,
so the duration badge vanished before the user ever saw the window. The fix
seeds each rebuilt row's duration from the editor's _duration_cache (the same
way thumbnails already survive a rebuild by re-reading the on-disk cache).

This builds a real EditorWindow on a temp folder with one finalised MKV, waits
for the initial probe to populate the duration, then rebuilds the list and
asserts the duration is still there. Without the fix, the post-rebuild duration
is None.

    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_duration_badge.py
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QThreadPool  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv[:1])

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui.recordings_list import _ROLE_DURATION  # noqa: E402
from media_fixture import make_momento_mkv  # noqa: E402

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def _make_finalised_mkv(path: Path) -> None:
    make_momento_mkv(path, with_audio=False)


def _row0_duration(ed: EditorWindow):
    model = ed._list._model
    if model.rowCount() == 0:
        return "no-rows"
    return model.item(0).data(_ROLE_DURATION)


def _pump_until(cond, timeout_s: float = 12.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        QThreadPool.globalInstance().waitForDone(50)
        _app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="momento_durbadge_"))
    rec = tmp / "Game_2026-01-01_120000.mkv"
    _make_finalised_mkv(rec)

    cfg = dataclasses.replace(load_config(), output_folder=str(tmp))
    ed = EditorWindow(cfg, session=None)  # __init__ lists + kicks the probe

    # 1) Initial probe must populate the duration (the badge appears).
    populated = _pump_until(
        lambda: isinstance(_row0_duration(ed), float) and _row0_duration(ed) > 0
    )
    d1 = _row0_duration(ed)
    check("badge: duration populated after the initial probe", populated)
    check("badge: initial duration is a positive float", isinstance(d1, float) and d1 > 0)

    # 2) Rebuild the list — exactly what the tray does when the prebuilt editor
    #    is opened. The duration must survive (seeded from _duration_cache),
    #    even though the metadata probe is skipped for the already-probed path.
    ed.refresh()
    _app.processEvents()
    d2 = _row0_duration(ed)
    check("badge: duration SURVIVES a list rebuild", isinstance(d2, float) and d2 > 0)
    check("badge: same duration value before and after rebuild", d1 == d2)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    code = main()
    # Tearing down a full EditorWindow (QMediaPlayer + native video surface)
    # can fault inside Qt's offscreen QPA during interpreter shutdown — after
    # the checks have already run. Exit hard so the process's exit code
    # reflects the test result, not a teardown artifact.
    sys.stdout.flush()
    import os as _os
    _os._exit(code)
