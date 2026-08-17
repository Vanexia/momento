r"""Verify tray close/reopen keeps the selected recording and playhead.

Run:
    C:\dev\Momento\.venv\Scripts\python.exe tests\smoke_tray_reopen_resume.py
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
    path = folder / name
    path.write_bytes(b"\x1aE\xdf\xa3 placeholder")
    os.utime(path, (mtime, mtime))
    return path


def main() -> int:
    app = QApplication(sys.argv)
    if app.platformName() == "offscreen":
        print("FAIL: running offscreen - close/show behaviour is unreliable.")
        return 2
    apply_dark_theme(app)

    tmp = Path(tempfile.mkdtemp(prefix="momento_resume_"))
    older = _mkv(tmp, "Game_2026-01-01_100000.mkv", 1000)
    _mkv(tmp, "Game_2026-01-02_100000.mkv", 2000)

    cfg = dataclasses.replace(
        load_config(),
        output_folder=str(tmp),
        close_to_tray=True,
    )
    ed = EditorWindow(cfg, session=None)
    ed.show()

    loaded = {"path": None}
    seeked: list[float] = []
    paused = {"called": False}

    def fake_load(path) -> None:
        loaded["path"] = Path(path) if path else None

    def fake_current_path():
        return loaded["path"]

    def fake_position() -> float:
        return 42.5

    def fake_seek(seconds: float) -> None:
        seeked.append(float(seconds))

    def fake_duration() -> float:
        return 120.0

    def fake_pause() -> None:
        paused["called"] = True

    result: dict[str, object] = {"ok": False}

    def select_and_patch() -> None:
        ed._list.select_by_path(older)
        loaded["path"] = older
        ed.preview.load = fake_load  # type: ignore[method-assign]
        ed.preview.current_path = fake_current_path  # type: ignore[method-assign]
        ed.preview.position = fake_position  # type: ignore[method-assign]
        ed.preview.seek = fake_seek  # type: ignore[method-assign]
        ed.preview.duration = fake_duration  # type: ignore[method-assign]
        ed.preview.pause = fake_pause  # type: ignore[method-assign]
        ed.close()
        ed._release_preview_if_parked()
        result["unloaded_while_hidden"] = loaded["path"] is None
        ed.show()
        ed.refresh(preserve_selection=True)
        QTimer.singleShot(900, check)

    def check() -> None:
        result["selected"] = ed._current_selection
        result["loaded"] = loaded["path"]
        result["paused"] = paused["called"]
        result["seeked"] = bool(seeked) and abs(seeked[-1] - 42.5) < 0.01
        ok = (
            result["selected"] == older
            and result["loaded"] == older
            and result["paused"]
            and result["unloaded_while_hidden"]
            and result["seeked"]
        )
        print(f"selected after reopen: {result['selected']}")
        print(f"loaded after reopen: {result['loaded']}")
        print(f"paused on close: {result['paused']}")
        print(f"unloaded while hidden: {result['unloaded_while_hidden']}")
        print(f"restored seek calls: {seeked}")
        print("-" * 60)
        result["ok"] = ok
        print("PASS" if ok else "FAIL")
        app.quit()

    QTimer.singleShot(700, select_and_patch)
    QTimer.singleShot(5000, app.quit)
    app.exec()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
