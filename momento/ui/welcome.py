"""First-run setup wizard.

Lightweight 8-step QStackedWidget wizard, NOT a parallel onboarding
system — it's the same dialog the app has always opened on first launch,
just rebuilt to walk new users through the live settings rather than just
explaining them.

Each step reads/writes the user's Config in-place via ``_pending``; on
Finish the dialog saves the new Config and emits ``settings_saved`` so
the tray's existing ``_apply_new_config`` slot reloads the session.

Skippable from any page. Re-openable from the editor's File menu so
returning users can revisit it; the auto-launch path stays gated on
``not config_path().exists()`` in ``__main__.py``.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from momento.config import Config, save_config
from momento.core.audio_loopback import list_loopback_devices
from momento.core.mic_capture import list_mic_devices
from momento.core.mic_monitor import MicMonitor
from momento.ui.level_meter import LevelMeter
from momento.ui.widgets import AnchoredComboBox
from momento.util.resources import app_icon_path, bookmark_sound_path

logger = logging.getLogger(__name__)


_STEP_TITLES = (
    "Welcome to Momento",
    "Choose a recordings folder",
    "Set up audio",
    "Choose capture quality",
    "Game monitoring",
    "Notifications & bookmarks",
    "Startup & tray",
    "You're all set",
)


class WelcomeDialog(QDialog):
    """First-run setup wizard.

    Replaces the old static welcome card. Same class name so the launcher
    in ``__main__.py`` doesn't move; the constructor now takes a Config
    and the dialog emits :py:attr:`settings_saved` with the user's choices
    on Finish.
    """

    # Emitted on Finish with the new Config (already persisted via
    # save_config). Mirrors SettingsPanel.settings_saved so the tray's
    # _apply_new_config slot works for both code paths.
    settings_saved = pyqtSignal(object)

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Momento — first-time setup")
        self.setModal(True)
        self.resize(640, 540)
        self.setMinimumSize(560, 480)
        icon_p = app_icon_path()
        if icon_p is not None:
            self.setWindowIcon(QIcon(str(icon_p)))

        self._config = config
        # Field-name → new value. Applied via dataclasses.replace on finish.
        self._pending: dict[str, object] = {}
        # Built lazily once the user lands on the Audio step.
        self._mic_monitor: MicMonitor | None = None
        self._sys_test_player: tuple[QMediaPlayer, QAudioOutput] | None = None
        # Cached device lists — re-enumerated on dialog open.
        self._mic_devices = list_mic_devices()
        self._sys_devices = list_loopback_devices()

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(14)

        # Header — step indicator + title.
        self._step_label = QLabel("")
        self._step_label.setStyleSheet("color: #8a92a3; font-size: 9pt;")
        self._title_label = QLabel("")
        title_font = QFont(self._title_label.font())
        title_font.setPointSize(18)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(self._step_label)
        header.addWidget(self._title_label)
        root.addLayout(header)

        # Steps.
        self._stack = QStackedWidget()
        builders = (
            self._build_welcome_page,
            self._build_folder_page,
            self._build_audio_page,
            self._build_capture_page,
            self._build_monitoring_page,
            self._build_notifications_page,
            self._build_startup_page,
            self._build_final_page,
        )
        for builder in builders:
            self._stack.addWidget(builder())
        root.addWidget(self._stack, stretch=1)

        # Footer — Back / Skip / Next.
        footer = QHBoxLayout()
        self._back_btn = QPushButton("Back")
        self._back_btn.clicked.connect(self._on_back)
        footer.addWidget(self._back_btn)
        footer.addStretch(1)
        self._skip_btn = QPushButton("Skip setup")
        self._skip_btn.clicked.connect(self._on_skip)
        footer.addWidget(self._skip_btn)
        self._next_btn = QPushButton("Next")
        self._next_btn.setObjectName("primary")
        self._next_btn.setDefault(True)
        self._next_btn.clicked.connect(self._on_next)
        footer.addWidget(self._next_btn)
        root.addLayout(footer)

        self._stack.currentChanged.connect(self._on_page_changed)
        self._on_page_changed(0)

    # ----------------------------------------------------------- navigation
    def _on_page_changed(self, index: int) -> None:
        total = self._stack.count()
        self._step_label.setText(f"Step {index + 1} of {total}")
        self._title_label.setText(_STEP_TITLES[index])
        self._back_btn.setEnabled(index > 0)
        self._next_btn.setText("Finish" if index == total - 1 else "Next")
        # The final page needs a refreshed checklist whenever we land on it.
        if index == total - 1:
            self._refresh_final_checklist()
        # Stop the mic test the moment we navigate away from the Audio page.
        if index != 2 and self._mic_monitor is not None and self._mic_monitor.is_running:
            self._mic_monitor.stop()
            self._mic_test_btn.setChecked(False)

    def _on_back(self) -> None:
        idx = self._stack.currentIndex()
        if idx > 0:
            self._stack.setCurrentIndex(idx - 1)

    def _on_next(self) -> None:
        idx = self._stack.currentIndex()
        if idx == self._stack.count() - 1:
            self._on_finish()
            return
        self._stack.setCurrentIndex(idx + 1)

    def _on_skip(self) -> None:
        """Jump straight to the final page so the user can still review their
        current settings + finish without abandoning the dialog mid-flow."""
        self._stack.setCurrentIndex(self._stack.count() - 1)

    def _on_finish(self) -> None:
        try:
            new_cfg = dataclasses.replace(self._config, **self._pending)
        except Exception:
            logger.exception("Wizard: building Config from pending values failed")
            QMessageBox.warning(
                self, "Momento",
                "Something went wrong applying your choices. Please open "
                "Settings to finish manually.",
            )
            self.reject()
            return
        try:
            save_config(new_cfg)
        except OSError as e:
            QMessageBox.critical(self, "Momento", f"Could not save settings:\n{e}")
            return
        self.settings_saved.emit(new_cfg)
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._mic_monitor is not None:
            self._mic_monitor.stop()
        super().closeEvent(event)

    # =============================================================== pages
    # ---- 1. Welcome ----
    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(12)
        col.addWidget(_para(
            "Momento sits in your system tray and watches for games. When "
            "one starts, it records the game window with your mic and "
            "system audio, and stops automatically when you close it."
        ))
        col.addWidget(_para(
            "Recordings stay on your PC. No accounts, no cloud, no upload."
        ))
        col.addWidget(_para(
            "This setup will take you through the basics. You can skip "
            "and come back to it later from the File menu."
        ))
        col.addStretch(1)
        return page

    # ---- 2. Recordings folder ----
    def _build_folder_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)
        col.addWidget(_para(
            "Pick where Momento should save your recordings. Files end up "
            "as ``.mkv`` here; exported clips go into the ``clips/`` "
            "subfolder."
        ))
        row = QHBoxLayout()
        self._folder_edit = QLineEdit(str(self._config.output_folder))
        self._folder_edit.textChanged.connect(
            lambda t: self._pending.update(output_folder=Path(t.strip()).expanduser())
        )
        row.addWidget(self._folder_edit, stretch=1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse_folder)
        row.addWidget(browse)
        col.addLayout(row)
        col.addWidget(_hint(
            "When the recordings folder hits its storage limit (Settings → "
            "Output), Momento deletes the oldest recordings first. Clips "
            "are kept."
        ))
        col.addStretch(1)
        return page

    def _on_browse_folder(self) -> None:
        start = self._folder_edit.text().strip() or str(Path.home())
        dlg = QFileDialog(self, "Choose recordings folder", start)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
        if dlg.exec() and dlg.selectedFiles():
            self._folder_edit.setText(dlg.selectedFiles()[0])

    # ---- 3. Audio ----
    def _build_audio_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(8)

        self._wizard_mic_combo = AnchoredComboBox()
        self._wizard_mic_combo.addItem("— none —", "")
        for d in self._mic_devices:
            self._wizard_mic_combo.addItem(d.name, d.id)
        _select_combo_by_value(self._wizard_mic_combo, self._config.mic_device)
        self._wizard_mic_combo.currentIndexChanged.connect(
            lambda _: self._pending.update(
                mic_device=self._wizard_mic_combo.currentData() or ""
            )
        )
        self._wizard_mic_combo.currentIndexChanged.connect(
            self._refresh_wizard_device_status
        )
        form.addRow("Microphone:", self._wizard_mic_combo)
        self._wizard_mic_status = QLabel("")
        self._wizard_mic_status.setStyleSheet("font-size: 9pt;")
        form.addRow("", self._wizard_mic_status)

        # Mic test row — reuses MicMonitor + LevelMeter.
        self._mic_test_btn = QPushButton("Test mic")
        self._mic_test_btn.setCheckable(True)
        self._mic_test_btn.setToolTip(
            "Plays your mic through the default speaker. Wear headphones "
            "to avoid feedback."
        )
        self._mic_test_btn.toggled.connect(self._on_mic_test_toggled)
        self._mic_meter = LevelMeter()
        self._mic_meter.setMinimumHeight(20)
        self._mic_caption = QLabel("")
        self._mic_caption.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
        self._mic_caption.setMinimumWidth(140)
        meter_label = QLabel("Mic input level")
        meter_label.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
        meter_block = QVBoxLayout()
        meter_block.setContentsMargins(0, 0, 0, 0)
        meter_block.setSpacing(2)
        meter_block.addWidget(meter_label)
        meter_block.addWidget(self._mic_meter)
        meter_block_wrap = QWidget()
        meter_block_wrap.setLayout(meter_block)
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.setSpacing(10)
        test_row.addWidget(self._mic_test_btn)
        test_row.addWidget(meter_block_wrap, stretch=1)
        test_row.addWidget(self._mic_caption)
        test_wrap = QWidget()
        test_wrap.setLayout(test_row)
        form.addRow("", test_wrap)

        # Silence-timer matches the Settings page behaviour — fires once a
        # few seconds in, flips caption to "No input detected" if quiet.
        from PyQt6.QtCore import QTimer
        self._mic_silence_timer = QTimer(self)
        self._mic_silence_timer.setSingleShot(True)
        self._mic_silence_timer.setInterval(4000)
        self._mic_silence_timer.timeout.connect(self._on_wizard_mic_silence)
        self._mic_input_seen = False

        # System audio.
        self._wizard_sys_combo = AnchoredComboBox()
        self._wizard_sys_combo.addItem("— none —", "")
        for d in self._sys_devices:
            self._wizard_sys_combo.addItem(d.name, d.id)
        _select_combo_by_value(self._wizard_sys_combo, self._config.system_audio_device)
        self._wizard_sys_combo.currentIndexChanged.connect(
            lambda _: self._pending.update(
                system_audio_device=self._wizard_sys_combo.currentData() or ""
            )
        )
        self._wizard_sys_combo.currentIndexChanged.connect(
            self._refresh_wizard_device_status
        )
        form.addRow("System audio:", self._wizard_sys_combo)
        self._wizard_sys_status = QLabel("")
        self._wizard_sys_status.setStyleSheet("font-size: 9pt;")
        form.addRow("", self._wizard_sys_status)
        form.addRow("", _hint(
            "Momento records whatever plays through the selected system "
            "audio device — speakers, headset, virtual cable."
        ))
        self._sys_test_btn = QPushButton("Test system audio")
        self._sys_test_btn.setToolTip(
            "Plays a short chime through the selected device. If you hear "
            "it, Momento can capture it."
        )
        self._sys_test_btn.clicked.connect(self._on_sys_test_clicked)
        form.addRow("", self._sys_test_btn)

        self._refresh_wizard_device_status()
        return page

    def _refresh_wizard_device_status(self) -> None:
        """Mirror the Settings → Audio "Connected / Not detected" badges."""
        def fmt(active_ids: set[str], current: str, label: QLabel) -> None:
            if not current:
                label.setText("✕ No device selected")
                label.setStyleSheet("color: #d4a64a; font-size: 9pt;")
                return
            if current in active_ids:
                label.setText("✓ Connected")
                label.setStyleSheet("color: #5cb85c; font-size: 9pt;")
            else:
                label.setText("✕ Not detected")
                label.setStyleSheet("color: #d4a64a; font-size: 9pt;")

        mic_ids = {d.id for d in self._mic_devices}
        sys_ids = {d.id for d in self._sys_devices}
        fmt(mic_ids, self._wizard_mic_combo.currentData() or "", self._wizard_mic_status)
        fmt(sys_ids, self._wizard_sys_combo.currentData() or "", self._wizard_sys_status)

    def _ensure_mic_monitor(self) -> MicMonitor:
        if self._mic_monitor is None:
            self._mic_monitor = MicMonitor(self)
            self._mic_monitor.level_changed.connect(self._mic_meter.set_level)
            self._mic_monitor.level_changed.connect(self._on_mic_level)
            self._mic_monitor.error.connect(self._on_mic_error)
            self._mic_monitor.stopped.connect(self._on_mic_stopped)
        return self._mic_monitor

    def _on_mic_test_toggled(self, on: bool) -> None:
        if on:
            mic_key = self._wizard_mic_combo.currentData() or ""
            if not mic_key:
                QMessageBox.information(
                    self, "Momento",
                    "Pick a microphone first, then start the test.",
                )
                self._mic_test_btn.setChecked(False)
                return
            self._ensure_mic_monitor().start(mic_key, monitor_to_speaker=True)
            self._mic_test_btn.setText("Stop test")
            self._mic_input_seen = False
            self._mic_silence_timer.start()
            self._mic_caption.setText("Listening…")
            self._mic_caption.setStyleSheet(
                "color: #b8c1d1; font-size: 9pt; font-style: italic;"
            )
        else:
            if self._mic_monitor is not None:
                self._mic_monitor.stop()
            self._mic_meter.reset()
            self._mic_test_btn.setText("Test mic")
            self._mic_caption.setText("")
            self._mic_silence_timer.stop()

    def _on_mic_level(self, level: float) -> None:
        if level > 0.05 and not self._mic_input_seen:
            self._mic_input_seen = True
            self._mic_silence_timer.stop()
            self._mic_caption.setText("Input detected")
            self._mic_caption.setStyleSheet(
                "color: #5cb85c; font-size: 9pt; font-weight: 600;"
            )

    def _on_wizard_mic_silence(self) -> None:
        if self._mic_input_seen or not self._mic_test_btn.isChecked():
            return
        self._mic_caption.setText("No input detected")
        self._mic_caption.setStyleSheet(
            "color: #d4a64a; font-size: 9pt; font-weight: 600;"
        )

    def _on_mic_error(self, message: str) -> None:
        QMessageBox.warning(self, "Momento", message)

    def _on_mic_stopped(self) -> None:
        self._mic_meter.reset()
        if self._mic_test_btn.isChecked():
            self._mic_test_btn.blockSignals(True)
            self._mic_test_btn.setChecked(False)
            self._mic_test_btn.blockSignals(False)
        self._mic_test_btn.setText("Test mic")
        self._mic_caption.setText("")
        self._mic_silence_timer.stop()

    def _on_sys_test_clicked(self) -> None:
        wav = bookmark_sound_path()
        if wav is None or not wav.exists():
            QMessageBox.warning(
                self, "Momento",
                "Bundled chime is missing — can't test system audio.",
            )
            return
        device_id = self._wizard_sys_combo.currentData() or ""
        target = None
        for dev in QMediaDevices.audioOutputs():
            try:
                dev_id_str = bytes(dev.id()).decode("utf-8", errors="ignore")
            except Exception:
                continue
            if device_id and device_id in dev_id_str:
                target = dev
                break
        player = QMediaPlayer(self)
        out = QAudioOutput(
            target if target is not None else QMediaDevices.defaultAudioOutput(),
            self,
        )
        out.setVolume(0.6)
        player.setAudioOutput(out)
        player.setSource(QUrl.fromLocalFile(str(wav)))
        player.play()
        # Hold a ref so Qt doesn't GC mid-playback.
        self._sys_test_player = (player, out)

    # ---- 4. Capture ----
    def _build_capture_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(8)

        self._res_combo = AnchoredComboBox()
        for label, value in (
            ("Match game (native)", "source"),
            ("1080p", "1080p"),
            ("1440p", "1440p"),
            ("4K", "4k"),
        ):
            self._res_combo.addItem(label, value)
        _select_combo_by_value(self._res_combo, self._config.target_resolution)
        self._res_combo.currentIndexChanged.connect(
            lambda _: self._pending.update(
                target_resolution=self._res_combo.currentData() or "source"
            )
        )
        form.addRow("Resolution:", self._res_combo)

        # FPS — mirrors the collapsed combo in Settings → Capture.
        from momento.util.screen import primary_refresh_rate
        detected = primary_refresh_rate(default=60)
        self._fps_combo = AnchoredComboBox()
        for label, value in (
            (f"Match display refresh rate ({detected} Hz)", -1),
            ("30 fps", 30),
            ("60 fps", 60),
            ("120 fps", 120),
        ):
            self._fps_combo.addItem(label, value)
        # Pick a sensible preset for the current config.
        if self._config.framerate_auto:
            idx = self._fps_combo.findData(-1)
        elif self._config.framerate in (30, 60, 120):
            idx = self._fps_combo.findData(self._config.framerate)
        else:
            idx = self._fps_combo.findData(-1)  # fall back to Match
        self._fps_combo.setCurrentIndex(max(0, idx))
        self._fps_combo.currentIndexChanged.connect(self._on_wizard_fps_changed)
        form.addRow("FPS:", self._fps_combo)

        self._quality_combo = AnchoredComboBox()
        for label, value in (
            ("Low (smaller files)", "low"),
            ("Medium", "medium"),
            ("High (recommended)", "high"),
        ):
            self._quality_combo.addItem(label, value)
        # Custom bitrate stays a Settings-only option — the wizard exposes
        # the three named presets so first-time users can pick quickly.
        _select_combo_by_value(
            self._quality_combo,
            self._config.quality_preset
            if self._config.quality_preset in {"low", "medium", "high"}
            else "high",
        )
        self._quality_combo.currentIndexChanged.connect(
            lambda _: self._pending.update(
                quality_preset=self._quality_combo.currentData() or "high"
            )
        )
        form.addRow("Quality:", self._quality_combo)

        form.addRow("", _hint(
            "These can be changed any time in Settings → Capture."
        ))
        return page

    def _on_wizard_fps_changed(self, _index: int) -> None:
        value = self._fps_combo.currentData()
        if value == -1:
            self._pending["framerate_auto"] = True
        elif isinstance(value, int) and value > 0:
            self._pending["framerate_auto"] = False
            self._pending["framerate"] = value

    # ---- 5. Game monitoring ----
    def _build_monitoring_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)
        col.addWidget(_para(
            "Momento watches your running processes for known games. When "
            "one starts, recording kicks off automatically — no hotkey to "
            "remember."
        ))
        self._monitor_check = QCheckBox("Start watching for games when Momento launches")
        self._monitor_check.setChecked(self._config.start_monitoring_on_launch)
        self._monitor_check.toggled.connect(
            lambda checked: self._pending.update(start_monitoring_on_launch=checked)
        )
        col.addWidget(self._monitor_check)
        col.addWidget(_hint(
            "The bundled list ships with ~650 popular titles. You can add, "
            "remove, scan running apps, or import/export the list from "
            "Settings → Games."
        ))
        col.addStretch(1)
        return page

    # ---- 6. Notifications + bookmarks ----
    def _build_notifications_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(8)

        col.addWidget(_para(
            "Pick which on-screen notifications you'd like to see. You can "
            "fine-tune these later in Settings → Notifications."
        ))
        self._notif_started = QCheckBox("Show \"Recording started\" when a game launches")
        self._notif_started.setChecked(self._config.show_recording_started_toast)
        self._notif_started.toggled.connect(
            lambda v: self._pending.update(show_recording_started_toast=v)
        )
        col.addWidget(self._notif_started)
        self._notif_saved = QCheckBox("Show \"Recording saved\" when a game exits")
        self._notif_saved.setChecked(self._config.show_recording_saved_toast)
        self._notif_saved.toggled.connect(
            lambda v: self._pending.update(show_recording_saved_toast=v)
        )
        col.addWidget(self._notif_saved)
        self._notif_bookmark = QCheckBox("Show \"Bookmark added\" when the hotkey lands")
        self._notif_bookmark.setChecked(self._config.show_bookmark_toast)
        self._notif_bookmark.toggled.connect(
            lambda v: self._pending.update(show_bookmark_toast=v)
        )
        col.addWidget(self._notif_bookmark)

        # Hotkey + bookmark explanation.
        col.addWidget(_para(
            "<b>Bookmarks</b> mark a moment on the recording's timeline so "
            "you can find good clips fast in the editor. The default "
            "hotkey is <b>F8</b>:"
        ))
        hk_row = QHBoxLayout()
        hk_row.setSpacing(8)
        hk_label = QLabel("Bookmark hotkey:")
        hk_label.setStyleSheet("color: #b8c1d1;")
        hk_row.addWidget(hk_label)
        self._hotkey_edit = QLineEdit(self._config.bookmark_hotkey)
        self._hotkey_edit.setMaximumWidth(180)
        self._hotkey_edit.setPlaceholderText("F8")
        self._hotkey_edit.textChanged.connect(
            lambda t: self._pending.update(
                bookmark_hotkey=t.strip() or "F8"
            )
        )
        hk_row.addWidget(self._hotkey_edit)
        hk_row.addStretch(1)
        col.addLayout(hk_row)

        col.addWidget(_hint(
            "Bookmarks appear as orange ticks on the editor timeline and "
            "as clickable chips below it."
        ))
        col.addStretch(1)
        return page

    # ---- 7. Startup ----
    def _build_startup_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(8)
        self._autostart_check = QCheckBox("Start Momento with Windows")
        self._autostart_check.setChecked(self._config.autostart_with_windows)
        self._autostart_check.toggled.connect(
            lambda v: self._pending.update(autostart_with_windows=v)
        )
        col.addWidget(self._autostart_check)
        self._monitor_launch_check = QCheckBox("Begin monitoring games on launch")
        self._monitor_launch_check.setChecked(self._config.start_monitoring_on_launch)
        self._monitor_launch_check.toggled.connect(
            lambda v: self._pending.update(start_monitoring_on_launch=v)
        )
        col.addWidget(self._monitor_launch_check)
        self._close_to_tray_check = QCheckBox("Close button minimises to tray")
        self._close_to_tray_check.setChecked(self._config.close_to_tray)
        self._close_to_tray_check.toggled.connect(
            lambda v: self._pending.update(close_to_tray=v)
        )
        col.addWidget(self._close_to_tray_check)
        col.addWidget(_hint(
            "When enabled, Momento starts in the system tray without "
            "opening the main window."
        ))
        col.addStretch(1)
        return page

    # ---- 8. Final ----
    def _build_final_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)
        col.addWidget(_para(
            "Here's what you've set up. Click Finish to save and start using "
            "Momento."
        ))
        self._final_list = QFrame()
        self._final_list.setFrameShape(QFrame.Shape.StyledPanel)
        self._final_list.setStyleSheet(
            "QFrame { background: #1d2027; border: 1px solid #262a33; border-radius: 6px; }"
        )
        final_lay = QVBoxLayout(self._final_list)
        final_lay.setContentsMargins(14, 12, 14, 12)
        final_lay.setSpacing(6)
        # Six checklist rows, populated by _refresh_final_checklist().
        self._final_labels: list[QLabel] = []
        for _ in range(6):
            lbl = QLabel("")
            lbl.setStyleSheet("color: #e6e8ee; font-size: 10pt;")
            lbl.setWordWrap(True)
            final_lay.addWidget(lbl)
            self._final_labels.append(lbl)
        col.addWidget(self._final_list)
        col.addWidget(_hint(
            "You can re-open this setup any time from the File menu → "
            "\"Run setup tutorial…\"."
        ))
        col.addStretch(1)
        return page

    def _refresh_final_checklist(self) -> None:
        """Render the six-item checklist from the merged pending+config state."""
        merged = dataclasses.replace(self._config, **self._pending)
        mic_ok = bool(merged.mic_device)
        sys_ok = bool(merged.system_audio_device)
        items = (
            (bool(merged.output_folder), f"Recordings folder: {merged.output_folder}"),
            (mic_ok, "Microphone selected" if mic_ok else "Microphone not set"),
            (sys_ok, "System audio selected" if sys_ok else "System audio not set"),
            (
                True,
                f"Capture: {_human_resolution(merged.target_resolution)} · "
                f"{_human_fps(merged)} · {_human_quality(merged.quality_preset)}",
            ),
            (
                merged.start_monitoring_on_launch,
                "Game monitoring starts on launch"
                if merged.start_monitoring_on_launch
                else "Game monitoring is paused on launch",
            ),
            (
                True,
                f"Bookmark hotkey: {merged.bookmark_hotkey or 'F8'}",
            ),
        )
        for label, (ok, text) in zip(self._final_labels, items):
            tick = "✓" if ok else "—"
            colour = "#5cb85c" if ok else "#d4a64a"
            label.setText(
                f"<span style='color:{colour}; font-weight:600'>{tick}</span>  {text}"
            )
            label.setTextFormat(Qt.TextFormat.RichText)


# ------------------------------------------------------------------ helpers
def _para(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setStyleSheet("color: #e6e8ee; font-size: 10pt; line-height: 1.4;")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
    return label


def _select_combo_by_value(combo: QComboBox, value: str) -> None:
    if not value:
        combo.setCurrentIndex(0)
        return
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            combo.setCurrentIndex(i)
            return
    # Unknown value — leave the first item selected; the caller will save
    # whatever the combo currently shows, not the stale value.


def _human_resolution(value: str) -> str:
    return {
        "source": "Match game",
        "1080p": "1080p",
        "1440p": "1440p",
        "4k": "4K",
    }.get(value, value)


def _human_fps(config: Config) -> str:
    if config.framerate_auto:
        return "Match display"
    return f"{config.framerate} fps"


def _human_quality(value: str) -> str:
    return {
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "custom": "Custom",
    }.get(value, value)
