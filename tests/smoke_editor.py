"""Smoke test: open the editor window standalone, auto-close after 1s."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.util.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)

    cfg = load_config()
    win = EditorWindow(cfg)
    win.show()

    # Auto-close after 1.5s so this is non-interactive
    QTimer.singleShot(1500, app.quit)

    rc = app.exec()
    print(f"PASS (rc={rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
