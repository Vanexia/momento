"""Capture a PNG of the SettingsPanel on a chosen tab.

    python tests/screenshot_settings.py out.png [tab_name]

tab_name matches the visible tab label (default "Startup").
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from momento.config import load_config
from momento.ui.settings_dialog import SettingsPanel
from momento.ui.theme import apply_dark_theme


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("settings.png")
    tab_name = sys.argv[2] if len(sys.argv) > 2 else "Startup"
    out.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    apply_dark_theme(app)
    cfg = load_config()
    panel = SettingsPanel(cfg)
    panel.resize(1300, 700)

    tabs = panel._tabs
    for i in range(tabs.count()):
        if tabs.tabText(i) == tab_name:
            tabs.setCurrentIndex(i)
            break
    panel.show()

    def shoot() -> None:
        panel.grab().save(str(out))
        print(f"saved {out}")
        app.quit()

    QTimer.singleShot(600, shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
