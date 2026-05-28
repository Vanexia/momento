"""System tray UI driven by SessionManager.

The status callback fires on the watcher thread; we marshal updates back to the
Qt main thread via a pyqtSignal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from momento.config import Config
from momento.core.game_names import humanise_game_name
from momento.core.game_watcher import ActiveGame
from momento.core.session import STATUS_IDLE, STATUS_RECORDING, SessionManager
from momento.ui.editor import EditorWindow
from momento.ui.toast import RecordingToast
from momento.util.hotkey import HotkeyError, HotkeyService
from momento.util.resources import app_icon_path, bookmark_sound_path

logger = logging.getLogger(__name__)


def make_tray_icon(recording: bool, size: int = 64) -> QIcon:
    """Build a small circle icon — filled red when recording, outlined otherwise."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(230, 230, 230))
        pen.setWidth(max(3, size // 16))
        painter.setPen(pen)
        if recording:
            painter.setBrush(QColor(220, 30, 30))
        else:
            painter.setBrush(QColor(60, 60, 60, 80))
        margin = size // 8
        painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    finally:
        painter.end()
    return QIcon(pixmap)


class MomentoTray(QObject):
    """Tray icon + menu. Marshals SessionManager status changes onto the Qt thread."""

    _status_signal = pyqtSignal(str, object)  # (status, ActiveGame|None)
    _failure_signal = pyqtSignal(str, object, str)  # (reason, ActiveGame, detail)
    _bookmark_signal = pyqtSignal(object, float)  # (ActiveGame, elapsed_seconds)

    def __init__(
        self,
        session: SessionManager,
        config: Config,
        parent: QObject | None = None,
        open_editor: Callable[[], None] | None = None,
        open_settings: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._config = config
        self._open_editor = open_editor
        self._open_settings = open_settings

        self._icon_idle = make_tray_icon(recording=False)
        self._icon_rec = make_tray_icon(recording=True)

        self._editor: EditorWindow | None = None
        self._hotkey_service: HotkeyService | None = None
        self._toast: RecordingToast | None = None
        self._bookmark_sound: QSoundEffect | None = None
        self._last_game_name: str | None = None  # so the stop-toast can name it

        self._tray = QSystemTrayIcon(self._icon_idle, self)
        self._tray.setToolTip("Momento — idle")

        # Tray menu — Momento's primary entry point. Recordings, editing,
        # devices etc. live inside the editor window; the tray surfaces the
        # status and the small set of "act on the daemon right now" actions
        # (toggle monitoring, stop the current recording, jump to the
        # recordings folder, quit).
        self._menu = QMenu()
        self._status_action = QAction("Status: Idle", self._menu)
        self._status_action.setEnabled(False)
        self._menu.addAction(self._status_action)
        self._menu.addSeparator()

        self._open_editor_action = QAction("Open Momento", self._menu)
        self._open_editor_action.triggered.connect(self._on_open_editor)
        self._menu.addAction(self._open_editor_action)

        self._open_folder_action = QAction("Open recordings folder", self._menu)
        self._open_folder_action.triggered.connect(self._on_open_folder)
        self._menu.addAction(self._open_folder_action)

        self._menu.addSeparator()
        self._stop_recording_action = QAction("Stop current recording", self._menu)
        # Hidden until a recording is actually running. The status callback
        # toggles visibility in _apply_status — a disabled-but-visible item
        # reads as "menu doesn't track state", which was confusing.
        self._stop_recording_action.setVisible(False)
        self._stop_recording_action.triggered.connect(self._on_stop_recording)
        self._menu.addAction(self._stop_recording_action)

        self._monitor_action = QAction("Pause monitoring", self._menu)
        self._monitor_action.triggered.connect(self._on_toggle_monitor)
        self._menu.addAction(self._monitor_action)

        self._menu.addSeparator()
        self._quit_action = QAction("Quit", self._menu)
        self._quit_action.triggered.connect(self._on_quit)
        self._menu.addAction(self._quit_action)

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        # Pull the monitor menu item's wording in line with the initial state.
        self._refresh_monitor_action()

        self._status_signal.connect(self._apply_status)
        self._failure_signal.connect(self._on_failure)
        self._bookmark_signal.connect(self._on_bookmark_qt)

    # ------------------------------------------------------------------ API
    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def set_hotkey_service(self, svc: HotkeyService | None) -> None:
        """Used so Settings saves can re-apply the bookmark hotkey live."""
        self._hotkey_service = svc

    def on_session_failure(self, reason: str, game: ActiveGame, detail: str) -> None:
        """Session callback for unrecoverable start failures — surface as a
        warning toast so the user sees it without needing to dig through logs.
        Marshalled onto the Qt thread via the existing status signal path."""
        self._failure_signal.emit(reason, game, detail)

    def on_bookmark_added(self, game: ActiveGame, elapsed_seconds: float) -> None:
        """Session callback for fresh bookmarks. Fires from the hotkey
        thread (Win32 WM_HOTKEY native event filter); marshal to the Qt
        thread before touching widgets."""
        self._bookmark_signal.emit(game, float(elapsed_seconds))

    def _on_failure(self, reason: str, game: ActiveGame, detail: str) -> None:
        # Gated by ``show_failure_toast`` (default on). Failures usually
        # need user action — silencing them is opt-in, not default.
        if not self._config.show_failure_toast:
            return
        try:
            display = humanise_game_name(game.exe_name) if game else "Momento"
            title = f"Couldn't record {display}"
            self._ensure_toast().show_warning(title, detail)
        except Exception:
            logger.exception("Failed to show failure toast")

    def on_session_status(self, status: str, game: ActiveGame | None) -> None:
        """Called from the watcher thread; emits a signal so the slot runs on the Qt thread."""
        self._status_signal.emit(status, game)

    # ------------------------------------------------------------------ slots
    def _apply_status(self, status: str, game: ActiveGame | None) -> None:
        if status == STATUS_RECORDING and game is not None:
            display_name = humanise_game_name(game.exe_name)
            label = f"Recording {display_name}"
            self._tray.setIcon(self._icon_rec)
            self._tray.setToolTip(f"Momento — recording {display_name}")
            self._last_game_name = display_name
            self._show_toast_recording(display_name)
        else:
            label = "Idle"
            self._tray.setIcon(self._icon_idle)
            self._tray.setToolTip("Momento — idle")
            # Only show the "saved" toast if we were actually mid-recording.
            if self._last_game_name is not None:
                self._show_toast_idle(self._last_game_name)
                self._last_game_name = None
        self._status_action.setText(f"Status: {label}")
        self._stop_recording_action.setVisible(status == STATUS_RECORDING)
        self._refresh_monitor_action()
        if self._editor is not None:
            self._editor.set_session_status(status, game)

    # ------------------------------------------------------------ toast
    def _ensure_toast(self) -> RecordingToast:
        if self._toast is None:
            self._toast = RecordingToast()
            icon_p = app_icon_path()
            if icon_p is not None:
                # QIcon picks the closest embedded resolution and gives us a
                # QPixmap at the size we ask for. Loading the .ico directly via
                # QPixmap only returns the first (16px) sub-image — that's why
                # the icon looked pixelated when the toast scaled it up.
                from PyQt6.QtCore import QSize
                icon = QIcon(str(icon_p))
                pix = icon.pixmap(QSize(128, 128))
                if not pix.isNull():
                    self._toast.set_app_icon(pix)
        # Position can change at any time via the Settings panel — keep
        # the toast in sync without forcing the user to relaunch.
        self._toast.set_position(self._config.notification_position)
        return self._toast

    def _show_toast_recording(self, display_name: str) -> None:
        if not self._config.show_recording_started_toast:
            return
        try:
            self._ensure_toast().show_recording(display_name)
        except Exception:
            logger.exception("Failed to show recording toast")

    def _show_toast_idle(self, display_name: str) -> None:
        if not self._config.show_recording_saved_toast:
            return
        try:
            self._ensure_toast().show_idle(display_name)
        except Exception:
            logger.exception("Failed to show idle toast")

    def _on_bookmark_qt(self, game: ActiveGame, elapsed_seconds: float) -> None:
        """Qt-thread handler for a fresh bookmark — show the orange toast
        and play the chime."""
        if self._config.show_bookmark_toast:
            try:
                display = humanise_game_name(game.exe_name) if game else None
                self._ensure_toast().show_bookmark(display, elapsed_seconds)
            except Exception:
                logger.exception("Failed to show bookmark toast")
        if self._config.bookmark_sound:
            try:
                self._ensure_bookmark_sound().play()
            except Exception:
                logger.exception("Failed to play bookmark chime")

    def _ensure_bookmark_sound(self) -> QSoundEffect:
        """Lazy-construct the QSoundEffect. Idempotent — loaded once, reused.

        Volume is attenuated to 0.5 here on top of the chime file's already
        soft ~0.18 peak amplitude. Net result is "audible over the game but
        not startling".
        """
        if self._bookmark_sound is None:
            eff = QSoundEffect(self)
            wav = bookmark_sound_path()
            if wav is not None:
                eff.setSource(QUrl.fromLocalFile(str(wav)))
                eff.setVolume(0.5)
                eff.setLoopCount(1)
            self._bookmark_sound = eff
        return self._bookmark_sound

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        logger.debug("Tray activated: reason=%s",
                     reason.name if hasattr(reason, "name") else reason)
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            # Some Windows themes deliver DoubleClick instead of Trigger on
            # left-click depending on click-speed settings. Treat the same.
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_open_editor()

    def _on_open_editor(self) -> None:
        if self._open_editor is not None:
            self._open_editor()
            return
        try:
            self._ensure_editor()
        except Exception:
            # Silent-construction-failures stranded the user with a tray
            # icon that did nothing on click. Logging it gives us a
            # fighting chance to diagnose if it ever happens again.
            logger.exception("Editor construction or show failed")
            return
        # Refresh in case new recordings landed since it was opened.
        self._editor.refresh()

    def _ensure_editor(self) -> EditorWindow:
        """Lazy-construct the editor window, wire its signals once, then show.

        Built + shown together on click (not pre-built hidden): showing a
        window that was constructed while hidden flashes when the preview's
        native QVideoWidget surface realizes on first paint. The heavy part
        (the settings panel) is deferred separately inside EditorWindow.
        """
        if self._editor is None:
            self._editor = EditorWindow(self._config, session=self._session)
            self._editor.settings_saved.connect(self._apply_new_config)
            # Seed the status strip with whatever we currently know — the
            # tray was the only listener until now, so the editor would
            # otherwise wait for the next session signal before showing
            # the right state. ActiveGame isn't exposed on SessionManager;
            # leave ``game`` None and let the next watcher tick refresh it.
            status = STATUS_RECORDING if self._session.is_recording else STATUS_IDLE
            self._editor.set_session_status(status, None)
        if not self._editor.isVisible():
            self._editor.show()
        else:
            self._editor.raise_()
            self._editor.activateWindow()
        return self._editor

    def _on_settings(self) -> None:
        if self._open_settings is not None:
            self._open_settings()
            return
        # Settings is now a page inside the editor window, not a separate
        # dialog — open (or focus) the editor and jump straight to it.
        editor = self._ensure_editor()
        editor.show_settings()

    def _apply_new_config(self, new_cfg: Config) -> None:
        self._config = new_cfg
        if self._editor is not None:
            self._editor._config = new_cfg  # let the editor see the new output folder
        try:
            self._session.reload_config(new_cfg)
        except Exception:
            logger.exception("Failed to apply new config to session")
            QMessageBox.warning(
                None, "Momento", "Settings saved, but applying them to the session failed."
            )
            return
        # Re-apply hotkey live if the spec changed.
        if (
            self._hotkey_service is not None
            and new_cfg.bookmark_hotkey != self._hotkey_service.current_hotkey()
        ):
            try:
                self._hotkey_service.set_hotkey(new_cfg.bookmark_hotkey)
            except HotkeyError as e:
                QMessageBox.warning(
                    None,
                    "Momento",
                    f"Settings saved, but the new bookmark hotkey couldn't be registered:\n{e}",
                )
        logger.info(
            "Settings saved: mic=%r system=%r output=%s hotkey=%r",
            new_cfg.mic_device, new_cfg.system_audio_device,
            new_cfg.output_folder, new_cfg.bookmark_hotkey,
        )

    def _on_quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ----------------------------------------------------------- new actions
    def _on_open_folder(self) -> None:
        import os
        folder = str(self._config.output_folder)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except OSError as e:
            logger.warning("Could not open recordings folder %s: %s", folder, e)

    def _on_stop_recording(self) -> None:
        try:
            self._session.stop_current_recording()
        except Exception:
            logger.exception("stop_current_recording raised")

    def _on_toggle_monitor(self) -> None:
        if self._session.is_monitoring:
            self._session.pause_monitoring()
        else:
            self._session.resume_monitoring()
        self._refresh_monitor_action()

    def _refresh_monitor_action(self) -> None:
        running = self._session.is_monitoring
        self._monitor_action.setText(
            "Pause monitoring" if running else "Resume monitoring"
        )


# helpers moved to momento.core.game_names.humanise_game_name
