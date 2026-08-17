"""Regression: credential refresh must not block the editor's GUI thread."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui import youtube_upload_dialog as upload_dialog  # noqa: E402
from momento.youtube import auth, client_config  # noqa: E402


def _pump_until(app, predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    original_connected = auth.is_connected
    original_credentials = auth.get_authorized_credentials
    original_matcher = auth.credentials_match_active_client
    original_client = client_config.load_active_client_config
    original_dialog = upload_dialog.YouTubeUploadDialog
    original_warning = QMessageBox.warning
    opened = {"count": 0}
    warnings = {"count": 0}
    credential_calls = {"count": 0}

    class _RejectedDialog:
        def __init__(self, *args, **kwargs) -> None:
            opened["count"] += 1

        def exec(self):
            return QDialog.DialogCode.Rejected

    def slow_credentials():
        credential_calls["count"] += 1
        time.sleep(0.4)
        return object()

    with tempfile.TemporaryDirectory() as folder:
        clip = Path(folder) / "clip.mp4"
        cfg = dataclasses.replace(load_config(), output_folder=folder)
        editor = EditorWindow(cfg, session=None)
        clip.write_bytes(b"clip")
        auth.is_connected = lambda: True
        auth.get_authorized_credentials = slow_credentials
        auth.credentials_match_active_client = lambda _creds: True
        client_config.load_active_client_config = lambda: object()
        upload_dialog.YouTubeUploadDialog = _RejectedDialog
        QMessageBox.warning = lambda *args, **kwargs: warnings.__setitem__(
            "count", warnings["count"] + 1
        )
        try:
            started = time.monotonic()
            editor._on_upload_to_youtube_requested(clip)
            elapsed = time.monotonic() - started
            prompt = elapsed < 0.1
            # Let the worker finish without pumping Qt. Its queued result has
            # not been consumed yet, so a second click must still be rejected.
            time.sleep(0.45)
            editor._on_upload_to_youtube_requested(clip)
            time.sleep(0.02)
            deduplicated = credential_calls["count"] == 1
            completed = _pump_until(app, lambda: opened["count"] == 1)
            time.sleep(0.45)
            app.processEvents()
            clean = warnings["count"] == 0
        finally:
            auth.is_connected = original_connected
            auth.get_authorized_credentials = original_credentials
            auth.credentials_match_active_client = original_matcher
            client_config.load_active_client_config = original_client
            upload_dialog.YouTubeUploadDialog = original_dialog
            QMessageBox.warning = original_warning
            editor.deleteLater()
            app.processEvents()

    print(f"{'PASS' if prompt else 'FAIL'} - upload action returned in {elapsed:.3f}s")
    print(f"{'PASS' if deduplicated else 'FAIL'} - overlapping credential checks are rejected")
    print(f"{'PASS' if completed else 'FAIL'} - upload dialog continued after refresh")
    print(f"{'PASS' if clean else 'FAIL'} - no spurious warning was shown")
    return 0 if prompt and deduplicated and completed and clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
