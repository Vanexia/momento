"""Friend-facing YouTube setup and upload-routing regression checks."""

from __future__ import annotations

import json
import io
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

import momento.ui.editor as editor_mod  # noqa: E402
import momento.ui.settings_dialog as settings_mod  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui.settings_dialog import SettingsPanel  # noqa: E402
from momento.youtube import auth, client_config  # noqa: E402


_app = QApplication.instance() or QApplication(sys.argv[:1])


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS - {label}")


def _desktop_config() -> dict[str, object]:
    return {
        "installed": {
            "client_id": "123456789012-friend.apps.googleusercontent.com",
            "project_id": "friend-project",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": "friend-example-secret",
            "redirect_uris": ["http://localhost"],
        }
    }


def main() -> int:
    originals = {
        "client_path": client_config.youtube_oauth_client_path,
        "youtube_dir": client_config.youtube_dir,
        "mic_devices": settings_mod.list_mic_devices,
        "loopback_devices": settings_mod.list_loopback_devices,
        "save_config": settings_mod.save_config,
        "file_dialog": QFileDialog.getOpenFileName,
        "settings_question": settings_mod.QMessageBox.question,
        "settings_information": settings_mod.QMessageBox.information,
        "settings_warning": settings_mod.QMessageBox.warning,
        "editor_information": editor_mod.QMessageBox.information,
        "editor_warning": editor_mod.QMessageBox.warning,
        "disconnect": auth.disconnect_account,
        "delete_avatar": auth.delete_cached_avatar,
        "connect_account": auth.connect_account,
    }
    panel = None
    editor = None
    with tempfile.TemporaryDirectory(prefix="momento-youtube-setup-") as folder:
        root = Path(folder)
        target = root / "appdata" / "youtube_oauth_client.dat"
        legacy_dir = root / "resources" / "youtube"
        legacy_dir.mkdir(parents=True)
        source = root / "desktop-client.json"
        source.write_text(json.dumps(_desktop_config()), encoding="utf-8")
        recording = root / "clip.mp4"

        client_config.youtube_oauth_client_path = lambda: target
        client_config.youtube_dir = lambda: legacy_dir
        settings_mod.list_mic_devices = lambda: []
        settings_mod.list_loopback_devices = lambda: []
        settings_mod.save_config = lambda _config: None
        messages: list[tuple[str, str]] = []
        opened_tabs: list[str | None] = []
        cleared = {"token": 0, "avatar": 0}
        settings_mod.QMessageBox.information = (
            lambda _parent, title, text, *_args: messages.append((title, text))
            or QMessageBox.StandardButton.Ok
        )
        settings_mod.QMessageBox.warning = (
            lambda _parent, title, text, *_args: messages.append((title, text))
            or QMessageBox.StandardButton.Ok
        )
        auth.disconnect_account = lambda: cleared.__setitem__("token", cleared["token"] + 1)
        auth.delete_cached_avatar = lambda: cleared.__setitem__("avatar", cleared["avatar"] + 1)

        try:
            panel = SettingsPanel(Config(output_folder=root))
            panel.open_tab("YouTube")
            check(
                "YouTube Settings page is always present",
                "YouTube" in panel._page_index
                and panel._stack.currentIndex() == panel._page_index["YouTube"],
            )
            check("Connect is disabled until an OAuth client is configured", not panel._yt_connect_btn.isEnabled())
            check(
                "setup controls have accessible labels and descriptions",
                panel._yt_import_btn.accessibleName() == "Import Google OAuth JSON"
                and bool(panel._yt_import_btn.accessibleDescription())
                and panel._yt_guide_btn.accessibleName() == "Open YouTube setup guide",
            )
            panel._yt_set_busy(True, "Signing in")
            check(
                "OAuth setup cannot be replaced or removed during browser sign-in",
                not panel._yt_import_btn.isEnabled()
                and not panel._yt_remove_client_btn.isEnabled(),
            )
            panel._yt_set_busy(False)

            private_value = "PRIVATE_OAUTH_CLIENT_VALUE_7F2"
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            settings_mod.logger.addHandler(handler)
            worker_failures: list[str] = []
            auth.connect_account = lambda: (_ for _ in ()).throw(
                RuntimeError(private_value)
            )
            worker = settings_mod._YouTubeConnectWorker()
            worker.failed.connect(worker_failures.append)
            try:
                worker.run()
            finally:
                settings_mod.logger.removeHandler(handler)
                auth.connect_account = originals["connect_account"]
            check(
                "OAuth worker logs and UI errors never echo client values",
                private_value not in stream.getvalue()
                and len(worker_failures) == 1
                and private_value not in worker_failures[0],
            )

            settings_mod.QFileDialog.getOpenFileName = lambda *_args, **_kwargs: ("", "")
            panel._on_yt_import_clicked()
            check("cancelling OAuth import has no side effect", not target.exists())

            settings_mod.QFileDialog.getOpenFileName = lambda *_args, **_kwargs: (str(source), "")
            panel._on_yt_import_clicked()
            check(
                "valid OAuth import enables Connect immediately",
                target.is_file() and panel._yt_connect_btn.isEnabled(),
            )
            check(
                "import clears credentials tied to any previous client",
                cleared == {"token": 1, "avatar": 1},
            )
            check(
                "import success explains protected storage",
                any("protected" in text.lower() and "delete" in text.lower() for _, text in messages),
            )

            editor = EditorWindow(Config(output_folder=root), session=None)
            recording.write_bytes(b"clip")
            check("editor Upload to YouTube action is always present", not editor._upload_btn.isHidden())
            embedded_settings = editor._ensure_settings_panel()
            original_epoch = editor._youtube_config_epoch
            embedded_settings.youtube_configuration_changed.emit()
            _app.processEvents()
            check(
                "editor invalidates pending credentials when OAuth setup changes",
                editor._youtube_config_epoch == original_epoch + 1,
            )
            stale_bridge = editor_mod._YouTubeCredentialsBridge()
            stale_bridge.config_epoch = original_epoch
            editor._youtube_auth_bridge = stale_bridge
            editor._youtube_upload_path = recording
            stale_uploads: list[object] = []
            editor._show_youtube_upload_dialog = (  # type: ignore[method-assign]
                lambda *_args: stale_uploads.append(object())
            )
            editor._on_youtube_credentials_ready(object(), None)
            check(
                "credentials loaded before a setup change cannot start an upload",
                not stale_uploads,
            )
            editor.show_settings = lambda tab=None: opened_tabs.append(tab)  # type: ignore[method-assign]
            editor_mod.QMessageBox.information = (
                lambda _parent, title, text, *_args: messages.append((title, text))
                or QMessageBox.StandardButton.Ok
            )
            editor_mod.QMessageBox.warning = (
                lambda _parent, title, text, *_args: messages.append((title, text))
                or QMessageBox.StandardButton.Ok
            )

            target.unlink()
            editor._on_upload_to_youtube_requested(recording)
            check(
                "unconfigured upload action opens YouTube setup",
                opened_tabs == ["YouTube"]
                and any("set up youtube" in title.lower() for title, _ in messages),
            )

            opened_tabs.clear()
            target.write_bytes(b"invalid protected setup")
            editor._on_upload_to_youtube_requested(recording)
            check(
                "invalid OAuth setup routes to repair instead of account sign-in",
                opened_tabs == ["YouTube"]
                and any("needs attention" in title.lower() for title, _ in messages),
            )

            target.unlink()
            client_config.import_user_client_config(source)
            panel._refresh_yt_setup_state()
            settings_mod.QMessageBox.question = (
                lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes
            )
            panel._on_yt_remove_client_clicked()
            check(
                "removing OAuth setup clears account state and disables Connect",
                not target.exists()
                and not panel._yt_connect_btn.isEnabled()
                and cleared == {"token": 2, "avatar": 2},
            )
        finally:
            if editor is not None:
                editor.close()
                editor.deleteLater()
            if panel is not None:
                panel.close()
                panel.deleteLater()
            _app.processEvents()
            client_config.youtube_oauth_client_path = originals["client_path"]
            client_config.youtube_dir = originals["youtube_dir"]
            settings_mod.list_mic_devices = originals["mic_devices"]
            settings_mod.list_loopback_devices = originals["loopback_devices"]
            settings_mod.save_config = originals["save_config"]
            settings_mod.QFileDialog.getOpenFileName = originals["file_dialog"]
            settings_mod.QMessageBox.question = originals["settings_question"]
            settings_mod.QMessageBox.information = originals["settings_information"]
            settings_mod.QMessageBox.warning = originals["settings_warning"]
            editor_mod.QMessageBox.information = originals["editor_information"]
            editor_mod.QMessageBox.warning = originals["editor_warning"]
            auth.disconnect_account = originals["disconnect"]
            auth.delete_cached_avatar = originals["delete_avatar"]
            auth.connect_account = originals["connect_account"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
