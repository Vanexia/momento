"""Capture a PNG of the editor at a chosen size + accent.

Used by the accent-variant comparison workflow. Invoke with the output
path as ``argv[1]`` and an optional ``--with-folder`` to point at a path
with recordings so the cards are visible:

    python tests/screenshot_editor.py C:/path/to/out.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from momento.config import load_config
from momento.ui.editor import EditorWindow
from momento.ui.theme import apply_dark_theme


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("editor.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    apply_dark_theme(app)
    cfg = load_config()
    # session=None so the status panel isn't built (it needs a live
    # SessionManager) — keeps the screenshot reproducible.
    ed = EditorWindow(cfg, session=None)
    ed.resize(1500, 820)
    ed.show()

    def shoot() -> None:
        pix = ed.grab()
        pix.save(str(out))
        print(f"saved {out}")
        app.quit()

    # Wait long enough for thumbnails / probes to populate the cards.
    QTimer.singleShot(1800, shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
