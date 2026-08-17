"""Open the real EditorWindow maximized and STAY open, for hands-on /
computer-use verification of the maximized->fullscreen->exit round-trip.

Unlike smoke_fullscreen_restore.py (which drives the toggle programmatically
and quits), this just shows the window so the transition can be watched live.
Drive it with: double-click the video to go fullscreen, Esc to exit.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui.theme import apply_dark_theme  # noqa: E402
from momento.util import windows_api  # noqa: E402
from momento.util.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    cfg = load_config()
    ed = EditorWindow(cfg, session=None)
    ed.setWindowTitle("Momento — fullscreen verify")
    ed.showMaximized()

    def _front() -> None:
        # Bypass the editor's opacity-0 anti-flash reveal + force this window
        # to the very front so a screenshot/computer-use sees it unambiguously
        # among the other pythonw windows (IDLE, the venv stub). Re-asserted on
        # a timer so the window stays foreground for hands-off driving — but
        # NOT while fullscreen (force-foregrounding the host can fight the
        # transition we're trying to observe).
        if ed.isFullScreen() or ed.preview.is_fullscreen():
            return
        ed.setWindowOpacity(1.0)
        ed.raise_()
        ed.activateWindow()
        windows_api.force_foreground_window(int(ed.winId()))

    QTimer.singleShot(400, _front)
    _t = QTimer()
    _t.timeout.connect(_front)
    _t.start(2000)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
