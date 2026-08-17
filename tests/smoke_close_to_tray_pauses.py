r"""Verify close-to-tray pauses the preview immediately.

Run:
    C:\dev\Momento\.venv\Scripts\python.exe tests\smoke_close_to_tray_pauses.py
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui.theme import apply_dark_theme  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    if app.platformName() == "offscreen":
        print("FAIL: running offscreen - close/hide behaviour is unreliable.")
        return 2
    apply_dark_theme(app)

    tmp = Path(tempfile.mkdtemp(prefix="momento_close_tray_"))
    cfg = dataclasses.replace(
        load_config(),
        output_folder=str(tmp),
        close_to_tray=True,
    )
    ed = EditorWindow(cfg, session=None)
    paused = {"called": False}

    def fake_pause() -> None:
        paused["called"] = True

    ed.preview.pause = fake_pause  # type: ignore[method-assign]
    ed.show()

    result: dict[str, bool] = {"ok": False}

    def close_editor() -> None:
        result["close_returned"] = ed.close()
        QTimer.singleShot(150, check)

    def check() -> None:
        result["paused"] = paused["called"]
        result["hidden"] = not ed.isVisible()
        print(f"close returned: {result.get('close_returned')}")
        print(f"preview paused: {result['paused']}")
        print(f"window hidden: {result['hidden']}")
        print("-" * 60)
        result["ok"] = result["paused"] and result["hidden"]
        print("PASS" if result["ok"] else "FAIL")
        app.quit()

    QTimer.singleShot(250, close_editor)
    QTimer.singleShot(4000, app.quit)
    app.exec()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
