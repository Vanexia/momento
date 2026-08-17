"""Smoke test: open the settings dialog standalone.

This actually shows the UI (so you can poke around). Cancel or save to exit.
Pass --auto to auto-close after 1 second without showing the window — useful
for headless verification that nothing crashes during construction.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.settings_dialog import SettingsPanel  # noqa: E402
from momento.ui.theme import apply_dark_theme  # noqa: E402
from momento.util.logging_setup import setup_logging  # noqa: E402
from momento.util.resources import app_icon_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="auto-close after 1s")
    args = parser.parse_args()

    setup_logging()
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    icon_p = app_icon_path()
    if icon_p is not None:
        app.setWindowIcon(QIcon(str(icon_p)))

    cfg = load_config()
    # SettingsPanel is now a regular QWidget that lives inside the editor —
    # for visual testing we host it in a plain top-level window.
    from PyQt6.QtWidgets import QMainWindow

    win = QMainWindow()
    win.setWindowTitle("Momento — Settings (test host)")
    if icon_p is not None:
        win.setWindowIcon(QIcon(str(icon_p)))
    panel = SettingsPanel(cfg)

    def on_saved(new_cfg) -> None:
        print(f"Saved: mic={new_cfg.mic_device!r} sys={new_cfg.system_audio_device!r}")

    def on_done() -> None:
        print("done")
        win.close()

    panel.settings_saved.connect(on_saved)
    panel.done.connect(on_done)
    win.setCentralWidget(panel)
    win.resize(760, 640)
    win.show()

    if args.auto:
        QTimer.singleShot(1000, win.close)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
