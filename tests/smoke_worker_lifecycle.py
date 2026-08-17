"""Background Settings work must not pin or abort the app during teardown."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def main() -> int:
    child = textwrap.dedent(
        """
        import os
        import time
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'

        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        from momento.config import Config
        import momento.ui.settings_dialog as settings_mod
        from momento.ui.editor import EditorWindow
        from momento.youtube import client_config

        def blocked_connect(self):
            time.sleep(0.4)
            self.failed.emit('simulated completion')

        settings_mod._YouTubeConnectWorker.run = blocked_connect
        client_config.has_configured_client = lambda: True
        app = QApplication([])
        editor = EditorWindow(Config())
        panel = editor._ensure_settings_panel()
        panel._on_yt_connect_clicked()
        print(f'update blocked: {editor.has_update_blocking_activity()}', flush=True)
        QTimer.singleShot(50, editor.deleteLater)
        QTimer.singleShot(800, app.quit)
        rc = app.exec()
        print(f'child completed: {rc}', flush=True)
        """
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        result = subprocess.run(
            [sys.executable, "-u", "-c", child],
            capture_output=True,
            text=True,
            env=env,
            timeout=6,
        )
    except subprocess.TimeoutExpired:
        print("FAIL - Settings teardown hung while OAuth worker was active")
        return 1

    ok = (
        result.returncode == 0
        and "update blocked: True" in result.stdout
        and "child completed: 0" in result.stdout
    )
    print(f"{'PASS' if ok else 'FAIL'} - Settings teardown survives active OAuth worker")
    if not ok:
        print(result.stdout)
        print(result.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
