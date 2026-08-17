"""Verify the editor's live list refresh: when a new recording lands, it
appears WITHOUT resetting the user's current selection / preview.

Uses a temp folder of placeholder .mkv files and a real (visible) EditorWindow
— refuses to run offscreen, where list/selection behaviour is unreliable.

    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_live_refresh.py
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui.theme import apply_dark_theme  # noqa: E402


def _mkv(folder: Path, name: str, mtime: int) -> Path:
    p = folder / name
    p.write_bytes(b"\x1aE\xdf\xa3 placeholder")  # EBML magic; enough to be listed
    os.utime(p, (mtime, mtime))
    return p


def main() -> int:
    app = QApplication(sys.argv)
    if app.platformName() == "offscreen":
        print("FAIL: running offscreen — list/selection behaviour is unreliable.")
        return 2
    apply_dark_theme(app)

    tmp = Path(tempfile.mkdtemp(prefix="momento_live_"))
    older = _mkv(tmp, "Game_2026-01-01_100000.mkv", 1000)
    _mkv(tmp, "Game_2026-01-02_100000.mkv", 2000)  # newer; row 0 on load

    cfg = dataclasses.replace(load_config(), output_folder=str(tmp))
    ed = EditorWindow(cfg, session=None)
    ed.show()

    r: dict[str, object] = {"ok": False}

    def step_select_older() -> None:
        r["rows_before"] = ed._list.row_count()
        # Simulate the user clicking the OLDER recording (not the default row 0).
        ed._list.select_by_path(older)  # emit=True -> drives the preview
        r["selected"] = ed._current_selection
        # A brand-new recording finishes and lands in the folder.
        new_recording = _mkv(tmp, "Game_2026-01-03_100000.mkv", 3000)
        # Live refresh, as the tray now does on recording-finish: update the
        # one finished path instead of rescanning the whole output folder.
        r["incremental_ok"] = ed.add_finished_recording(
            new_recording, preserve_selection=True
        )
        QTimer.singleShot(250, step_check)

    def step_check() -> None:
        r["rows_after"] = ed._list.row_count()
        r["selected_after"] = ed._current_selection
        _report()
        app.quit()

    def _report() -> None:
        new_appeared = r.get("rows_after") == (r.get("rows_before") or 0) + 1
        kept = (
            r.get("selected") is not None
            and str(r.get("selected_after")) == str(r.get("selected")) == str(older)
        )
        print(f"rows: {r.get('rows_before')} -> {r.get('rows_after')} (new recording appears: {new_appeared})")
        print(f"selection: {r.get('selected')} -> {r.get('selected_after')} (preserved: {kept})")
        print(f"incremental update accepted: {r.get('incremental_ok')}")
        print("-" * 60)
        r["ok"] = bool(new_appeared and kept and r.get("incremental_ok"))
        print("PASS" if r["ok"] else "FAIL")

    QTimer.singleShot(700, step_select_older)
    QTimer.singleShot(4000, app.quit)  # safety net
    app.exec()
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
