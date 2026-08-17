"""Smoke test: launch the full tray app, wait briefly, quit cleanly.

Verifies the QApplication + tray + session wiring stands up without throwing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from momento.config import Config  # noqa: E402
from momento.core.session import SessionManager  # noqa: E402
from momento.ui.tray import MomentoTray  # noqa: E402
from momento.util.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("FAIL: system tray not available")
        return 2

    config = Config()
    session = SessionManager(config)
    tray = MomentoTray(session, config)
    session.set_status_callback(tray.on_session_status)
    tray.on_session_status("idle", None)
    tray.show()
    session.start()

    # Emit a synthetic 'recording' status to exercise the icon swap.
    from momento.core.game_watcher import ActiveGame

    QTimer.singleShot(300, lambda: tray.on_session_status(
        "recording", ActiveGame(exe_name="smoketest.exe", pid=0, exe_path=None)
    ))
    QTimer.singleShot(700, lambda: tray.on_session_status("idle", None))
    QTimer.singleShot(1200, app.quit)

    rc = app.exec()
    session.shutdown()
    print(f"PASS (app rc={rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
