"""Settings panel — embedded inside the main editor window via QStackedWidget.

Save flow:
  1. Build a new Config from form values
  2. Persist it via save_config(...)
  3. Apply the autostart toggle to the registry
  4. Emit ``settings_saved`` so the tray reloads the session
  5. Emit ``done`` so the host window can switch back to the editor view
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import (
    QObject, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from PyQt6.QtGui import QBrush, QColor, QFont, QIcon

from momento.config import Config, DEFAULT_KNOWN_GAMES, save_config
from momento.core.audio_loopback import LoopbackDevice, list_loopback_devices
from momento.core.game_names import humanise_game_name
from momento.core.mic_capture import MicDevice, list_mic_devices
from momento.core.mic_monitor import MicMonitor
from momento.core.storage_cleanup import MigrationWorker, count_movable, migrate_to_folder
from momento.ui.level_meter import LevelMeter
from momento.ui.widgets import AnchoredComboBox
from momento.util.autostart import set_autostart
from momento.util.format import format_bytes, free_bytes_for
from momento.util.paths import window_state_path
from momento.util.windows_api import logical_drives
from momento.util.hotkey import HotkeyError, parse_hotkey
from momento.util.resources import app_icon_path

logger = logging.getLogger(__name__)


class _MigrationDriver(QObject):
    """Thread-side adapter: wraps :class:`MigrationWorker` so its
    ``progress_callback`` becomes a Qt signal the dialog can subscribe to."""

    progress_changed = pyqtSignal(int, int, str)
    finished = pyqtSignal(int, int)

    def __init__(
        self,
        worker,
        pairs: list,
    ) -> None:
        super().__init__()
        self._worker = worker
        self._pairs = pairs

    def run(self) -> None:
        moved, failed = self._worker.run(
            pairs=self._pairs,
            progress_callback=lambda d, t, n: self.progress_changed.emit(d, t, n),
        )
        self.finished.emit(moved, failed)


class SettingsPanel(QWidget):
    """A QWidget that hosts the settings UI in-place inside the main window.

    Signals:
      * ``settings_saved(Config)``  — fired AFTER successful save to disk.
      * ``done()``                  — fired when the user is finished, whether
                                       via Save or Back; the host should swap
                                       back to its main view.
    """

    settings_saved = pyqtSignal(object)  # emits the new Config
    done = pyqtSignal()

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._config = config
        self._mic_devices: list[MicDevice] = []
        self._sys_devices: list[LoopbackDevice] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Header row: back button on the left, page title in the middle.
        header = QHBoxLayout()
        header.setSpacing(8)
        self._back_btn = QToolButton()
        self._back_btn.setText("← Back")
        self._back_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._on_back)
        header.addWidget(self._back_btn)
        title = QLabel("Settings")
        title.setStyleSheet("font-size: 13pt; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_audio_tab(), "Audio")
        self._tabs.addTab(self._build_capture_tab(), "Capture")
        self._tabs.addTab(self._build_output_tab(), "Output")
        self._tabs.addTab(self._build_bookmarks_tab(), "Bookmarks")
        self._tabs.addTab(self._build_games_tab(), "Games")
        self._tabs.addTab(self._build_notifications_tab(), "Notifications")
        self._tabs.addTab(self._build_startup_tab(), "Startup")
        self._tabs.addTab(self._build_youtube_tab(), "YouTube")
        root.addWidget(self._tabs, stretch=1)

        self._populate_devices()
        self._load_from_config()

    # ----------------------------------------------------------- public API
    def reload_from_config(self, config: Config) -> None:
        """Reset the form to match the supplied Config (used when the host
        re-shows the panel and wants any unsaved edits discarded)."""
        self._config = config
        self._populate_devices()
        self._load_from_config()

    def open_tab(self, name: str) -> None:
        """Switch to a named tab (case-insensitive). No-op if name unknown."""
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i).lower() == name.lower():
                self._tabs.setCurrentIndex(i)
                return

    # ----------------------------------------------------------- nav
    def _on_back(self) -> None:
        self._stop_mic_test()
        self.done.emit()

    def _stop_mic_test(self) -> None:
        """Make sure the mic-test worker isn't left running when the user
        leaves the panel — soundcard's recorder holds the device handle."""
        if self._mic_test_btn.isChecked():
            self._mic_test_btn.setChecked(False)
        elif self._mic_monitor is not None and self._mic_monitor.is_running:
            self._mic_monitor.stop()

    # ---------------------------------------------------------------- tabs
    def _build_audio_tab(self) -> QWidget:
        return _tab_with(
            self._build_audio_group(), on_save=self._on_save, on_cancel=self._on_back
        )

    def _build_capture_tab(self) -> QWidget:
        return _tab_with_groups(
            self._build_capture_group(),
            _tips_group(
                "Recommended for most users",
                [
                    "Match game resolution · 60 FPS or Match display "
                    "refresh rate · High quality.",
                ],
            ),
            _tips_group(
                "Capture tips",
                [
                    "Higher framerates need more GPU and produce larger files. "
                    "Match-the-display is a sensible default for most games.",
                    "Recording locks to the game's native window size — no "
                    "downscale, no upscale.",
                    "MKV is crash-safe: if Windows panics mid-game, the "
                    "partial recording is still playable.",
                ],
            ),
            on_save=self._on_save,
            on_cancel=self._on_back,
        )

    def _build_output_tab(self) -> QWidget:
        return _tab_with(
            self._build_output_group(), on_save=self._on_save, on_cancel=self._on_back
        )

    def _build_bookmarks_tab(self) -> QWidget:
        return _tab_with_groups(
            self._build_bookmark_group(),
            _tips_group(
                "How bookmarks work",
                [
                    "Bookmarks are saved per-recording, alongside the file. "
                    "Deleting a recording deletes its bookmarks too.",
                    "In the editor, bookmarks appear as orange ticks on the "
                    "timeline and as clickable chips below it.",
                    "The chime, if enabled, plays through your default "
                    "output — which means it lands in the recording too.",
                ],
            ),
            on_save=self._on_save,
            on_cancel=self._on_back,
        )

    def _build_games_tab(self) -> QWidget:
        return _tab_with(
            self._build_games_group(),
            stretch_last=True,
            max_width=_GAMES_SETTINGS_WIDTH,
            on_save=self._on_save,
            on_cancel=self._on_back,
        )

    def _build_notifications_tab(self) -> QWidget:
        return _tab_with(
            self._build_notifications_group(), on_save=self._on_save, on_cancel=self._on_back
        )

    def _build_startup_tab(self) -> QWidget:
        return _tab_with(
            self._build_startup_group(), on_save=self._on_save, on_cancel=self._on_back
        )

    def _build_youtube_tab(self) -> QWidget:
        return _tab_with_groups(
            self._build_youtube_account_group(),
            self._build_youtube_defaults_group(),
            on_save=self._on_save,
            on_cancel=self._on_back,
        )

    # ---------------------------------------------------------------- build
    def _build_audio_group(self) -> QGroupBox:
        box = QGroupBox("Audio")
        layout = QFormLayout(box)

        self._mic_combo = AnchoredComboBox()
        self._mic_combo.setMinimumWidth(320)
        self._mic_combo.currentIndexChanged.connect(self._refresh_device_status_labels)
        self._mic_status_label = QLabel("")
        self._mic_status_label.setStyleSheet("font-size: 9pt;")
        self._mic_slider, self._mic_vol_spin, mic_vol_row = _slider_with_spin(0, 200, 100)

        # Mic test row: a "Test mic" toggle + a live level meter. The button
        # opens the configured mic, plays it through the default speaker,
        # and pushes peak amplitudes to the meter. Off by default; only
        # active while the user is on this tab.
        self._mic_test_btn = QPushButton("Test mic")
        self._mic_test_btn.setCheckable(True)
        self._mic_test_btn.setToolTip(
            "Plays your mic through the default speaker so you can hear "
            "yourself. Wear headphones — speakers will produce feedback. "
            "Click again to stop."
        )
        self._mic_test_btn.toggled.connect(self._on_mic_test_toggled)
        self._mic_meter = LevelMeter()
        self._mic_meter.setMinimumHeight(20)
        self._mic_meter.setToolTip(
            "Mic peak level. The bar only moves while Test mic is running."
        )
        # Caption tracks three states: Listening… (warm-up), Input detected
        # (peak above threshold), No input detected (silent for ~4s).
        self._mic_status_caption = QLabel("")
        self._mic_status_caption.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
        self._mic_status_caption.setMinimumWidth(140)
        # Small label above the meter so it reads as a level meter, not an
        # interactive input field.
        meter_label = QLabel("Mic input level")
        meter_label.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
        meter_block = QVBoxLayout()
        meter_block.setContentsMargins(0, 0, 0, 0)
        meter_block.setSpacing(2)
        meter_block.addWidget(meter_label)
        meter_block.addWidget(self._mic_meter)
        meter_block_wrap = QWidget()
        meter_block_wrap.setLayout(meter_block)
        mic_test_row = QHBoxLayout()
        mic_test_row.setContentsMargins(0, 0, 0, 0)
        mic_test_row.setSpacing(10)
        mic_test_row.addWidget(self._mic_test_btn)
        mic_test_row.addWidget(meter_block_wrap, stretch=1)
        mic_test_row.addWidget(self._mic_status_caption)
        mic_test_wrap = QWidget()
        mic_test_wrap.setLayout(mic_test_row)
        # Triggered once a few seconds after the test starts; if the live peak
        # hasn't crossed the threshold by then, the caption switches to
        # "No input detected" so the user gets useful feedback.
        from PyQt6.QtCore import QTimer
        self._mic_silence_timer = QTimer(self)
        self._mic_silence_timer.setSingleShot(True)
        self._mic_silence_timer.setInterval(4000)
        self._mic_silence_timer.timeout.connect(self._on_mic_silence)
        self._mic_input_seen = False

        # The monitor itself outlives any single click of the button —
        # constructed once per SettingsPanel, started/stopped on demand.
        self._mic_monitor: MicMonitor | None = None

        self._audio_combo = AnchoredComboBox()
        self._audio_combo.setMinimumWidth(320)
        self._audio_combo.currentIndexChanged.connect(self._refresh_device_status_labels)
        self._sys_status_label = QLabel("")
        self._sys_status_label.setStyleSheet("font-size: 9pt;")
        sys_hint = _hint_label(
            "Choose the speakers or headset you want Momento to record. "
            "Momento captures whatever plays through this device."
        )
        self._sys_slider, self._sys_vol_spin, sys_vol_row = _slider_with_spin(0, 200, 100)

        # System-audio test button — plays the bookmark chime through the
        # configured playback device so the user can confirm a) the device
        # is alive and b) they picked the right one.
        self._sys_test_btn = QPushButton("Test system audio")
        self._sys_test_btn.setToolTip(
            "Plays a short chime through the selected device. If you hear "
            "the chime, that's the device Momento will record from."
        )
        self._sys_test_btn.clicked.connect(self._on_sys_audio_test_clicked)
        # Lazily built when first needed so we don't load wav data unless
        # the user clicks the button.
        self._sys_test_sound = None

        self._refresh_btn = QPushButton("Refresh device list")
        self._refresh_btn.clicked.connect(self._populate_devices)

        layout.addRow("Microphone:", self._mic_combo)
        layout.addRow("", self._mic_status_label)
        layout.addRow("Mic volume:", mic_vol_row)
        layout.addRow("", mic_test_wrap)
        layout.addRow("System audio:", self._audio_combo)
        layout.addRow("", self._sys_status_label)
        layout.addRow("", sys_hint)
        layout.addRow("System volume:", sys_vol_row)
        layout.addRow("", self._sys_test_btn)
        layout.addRow("", self._refresh_btn)
        # NOTE: Config.audio_offset_ms is intentionally NOT exposed as a UI
        # field. The default (-50 ms) handles typical WASAPI loopback
        # latency; users with unusual hardware can override via the JSON
        # config. Most people shouldn't have to think about this.
        return box

    def _refresh_device_status_labels(self) -> None:
        """Surface "Connected" / "Not detected" next to each device combo so
        the user can tell at a glance whether the saved device id resolves
        to a real device today."""
        def fmt(active_ids: set[str], current: str, label: QLabel) -> None:
            if not current:
                label.setText("✕ No device selected")
                label.setStyleSheet("color: #d4a64a; font-size: 9pt;")
                return
            if current in active_ids:
                label.setText("✓ Connected")
                label.setStyleSheet("color: #5cb85c; font-size: 9pt;")
            else:
                label.setText("✕ Not detected — pick another device above")
                label.setStyleSheet("color: #d4a64a; font-size: 9pt;")

        mic_ids = {d.id for d in self._mic_devices}
        sys_ids = {d.id for d in self._sys_devices}
        fmt(mic_ids, self._mic_combo.currentData() or "", self._mic_status_label)
        fmt(sys_ids, self._audio_combo.currentData() or "", self._sys_status_label)

    def _on_sys_audio_test_clicked(self) -> None:
        """Play the bookmark chime through the configured playback device."""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

        from momento.util.resources import bookmark_sound_path

        wav = bookmark_sound_path()
        if wav is None or not wav.exists():
            QMessageBox.warning(
                self, "Momento",
                "Bundled chime is missing — can't test system audio.",
            )
            return

        device_id = self._audio_combo.currentData() or ""
        target = None
        for dev in QMediaDevices.audioOutputs():
            if bytes(dev.id()).decode("utf-8", errors="ignore") == device_id:
                target = dev
                break
            try:
                # soundcard ids look like "{guid}.{guid}"; QMediaDevices ids
                # usually include the guid as a substring. Fall back to a
                # contains-check if the exact match fails.
                if device_id and device_id in bytes(dev.id()).decode("utf-8", errors="ignore"):
                    target = dev
                    break
            except Exception:
                pass
        # Build a transient player so the chime can finish even if the user
        # navigates away from the tab.
        player = QMediaPlayer(self)
        out = QAudioOutput(target if target is not None else QMediaDevices.defaultAudioOutput(), self)
        out.setVolume(0.6)
        player.setAudioOutput(out)
        player.setSource(QUrl.fromLocalFile(str(wav)))
        player.play()
        # Hold references so Qt doesn't GC them mid-playback.
        self._sys_test_sound = (player, out)

    # ----------------------------------------------------------- mic test
    def _ensure_mic_monitor(self) -> MicMonitor:
        if self._mic_monitor is None:
            self._mic_monitor = MicMonitor(self)
            self._mic_monitor.level_changed.connect(self._mic_meter.set_level)
            self._mic_monitor.level_changed.connect(self._update_mic_caption)
            self._mic_monitor.error.connect(self._on_mic_monitor_error)
            self._mic_monitor.stopped.connect(self._on_mic_monitor_stopped)
        return self._mic_monitor

    def _update_mic_caption(self, level: float) -> None:
        """Once any sample crosses the noise floor, switch to "Input detected"
        and cancel the silence timer."""
        if level > 0.05 and not self._mic_input_seen:
            self._mic_input_seen = True
            self._mic_silence_timer.stop()
            self._mic_status_caption.setText("Input detected")
            self._mic_status_caption.setStyleSheet(
                "color: #5cb85c; font-size: 9pt; font-weight: 600;"
            )

    def _on_mic_silence(self) -> None:
        """Silence-timer fires when no audible input has been seen — only
        flips the caption if the user hasn't already had a louder sample."""
        if self._mic_input_seen:
            return
        if not self._mic_test_btn.isChecked():
            return
        self._mic_status_caption.setText("No input detected")
        self._mic_status_caption.setStyleSheet(
            "color: #d4a64a; font-size: 9pt; font-weight: 600;"
        )

    def _on_mic_test_toggled(self, on: bool) -> None:
        if on:
            mic_key = self._mic_combo.currentData() or ""
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
            self._mic_status_caption.setText("Listening…")
            self._mic_status_caption.setStyleSheet(
                "color: #b8c1d1; font-size: 9pt; font-style: italic;"
            )
        else:
            if self._mic_monitor is not None:
                self._mic_monitor.stop()
            self._mic_meter.reset()
            self._mic_test_btn.setText("Test mic")
            self._mic_status_caption.setText("")
            self._mic_silence_timer.stop()

    def _on_mic_monitor_error(self, message: str) -> None:
        QMessageBox.warning(self, "Momento", message)

    def _on_mic_monitor_stopped(self) -> None:
        """Worker thread exited (clean or error). Make sure the button + meter
        end up in the right state regardless of which side stopped first."""
        self._mic_meter.reset()
        if self._mic_test_btn.isChecked():
            self._mic_test_btn.blockSignals(True)
            self._mic_test_btn.setChecked(False)
            self._mic_test_btn.blockSignals(False)
        self._mic_test_btn.setText("Test mic")
        self._mic_status_caption.setText("")
        self._mic_silence_timer.stop()

    def _build_capture_group(self) -> QGroupBox:
        box = QGroupBox("Capture")
        layout = QFormLayout(box)

        # ----- Resolution -----
        self._resolution_combo = AnchoredComboBox()
        for label, value in (
            ("Match game (native — no scaling)", "source"),
            ("1080p (1920×1080)", "1080p"),
            ("1440p (2560×1440)", "1440p"),
            ("4K (3840×2160)", "4k"),
        ):
            self._resolution_combo.addItem(label, value)
        self._resolution_combo.setToolTip(
            "Non-source presets downscale during encode (smaller files, less "
            "GPU). Momento never upscales — picking 4K with a 1080p game "
            "still records at 1080p."
        )
        layout.addRow("Resolution:", self._resolution_combo)

        # ----- Framerate -----
        # Single "FPS" combo is the user-facing control. The auto-match
        # checkbox + manual spinner still back the config fields, but the
        # combo drives them — they only surface as the labelled custom
        # row beneath, and only when "Custom…" is selected.
        from momento.util.screen import primary_refresh_rate
        detected = primary_refresh_rate(default=60)
        self._detected_refresh_rate = detected

        self._framerate_auto_check = QCheckBox()
        self._framerate_auto_check.toggled.connect(self._on_framerate_auto_toggled)
        self._framerate_auto_check.setVisible(False)  # backing state only

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(24, 240)
        self._fps_spin.setSuffix(" fps")

        self._fps_preset_combo = AnchoredComboBox()
        # Sentinel -1 = match display; -2 = custom (reveals the spinner).
        for label, value in (
            (f"Match display refresh rate ({detected} Hz)", -1),
            ("30 fps", 30),
            ("60 fps", 60),
            ("120 fps", 120),
            ("Custom…", -2),
        ):
            self._fps_preset_combo.addItem(label, value)
        self._fps_preset_combo.currentIndexChanged.connect(self._on_fps_preset_changed)
        layout.addRow("FPS:", self._fps_preset_combo)

        # Custom-fps row — hidden unless the combo lands on "Custom…".
        self._custom_fps_row_label = QLabel("Custom FPS:")
        layout.addRow(self._custom_fps_row_label, self._fps_spin)
        self._custom_fps_row_label.setVisible(False)
        self._fps_spin.setVisible(False)

        # ----- Quality -----
        self._quality_combo = AnchoredComboBox()
        for label, value in (
            ("Low", "low"),
            ("Medium", "medium"),
            ("High (recommended)", "high"),
            ("Custom bitrate", "custom"),
        ):
            self._quality_combo.addItem(label, value)
        self._quality_combo.setToolTip(
            "Low/Medium/High map to NVENC constant-quality (CQ) values. "
            "Custom switches to a fixed bitrate target."
        )
        self._quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        layout.addRow("Quality:", self._quality_combo)
        # Selection-driven description — opaque "Low/Medium/High" labels
        # need a hint about what they mean visually + on disk. Seeded
        # with the default selection so it isn't blank on first paint
        # (currentIndexChanged doesn't fire when setCurrentIndex matches
        # the existing index).
        self._quality_desc_label = _hint_label(
            _QUALITY_DESCRIPTIONS.get(
                self._quality_combo.currentData() or "", ""
            )
        )
        layout.addRow("", self._quality_desc_label)

        self._bitrate_spin = QSpinBox()
        self._bitrate_spin.setRange(1_000, 200_000)
        self._bitrate_spin.setSingleStep(500)
        self._bitrate_spin.setSuffix(" kbit/s")
        self._bitrate_spin.setMinimumWidth(160)
        self._bitrate_spin.setToolTip(
            "Target bitrate when Quality is Custom. 1080p60 looks good around "
            "8–12 Mbit/s; 4K60 wants ~25–40 Mbit/s. NVENC handles this with "
            "CBR — file size scales linearly with duration."
        )
        # Explicit label widget so the whole row can be hidden when Quality
        # isn't Custom (mirrors the Custom FPS row pattern above).
        self._bitrate_row_label = QLabel("Custom bitrate:")
        layout.addRow(self._bitrate_row_label, self._bitrate_spin)
        self._bitrate_row_label.setVisible(False)
        self._bitrate_spin.setVisible(False)

        return box

    def _on_fps_preset_changed(self, _index: int) -> None:
        """Apply an FPS preset + reveal the Custom row only when needed."""
        value = self._fps_preset_combo.currentData()
        is_custom = value == -2
        self._custom_fps_row_label.setVisible(is_custom)
        self._fps_spin.setVisible(is_custom)
        if value == -1:  # Match display refresh rate
            self._framerate_auto_check.setChecked(True)
            return
        if is_custom:
            self._framerate_auto_check.setChecked(False)
            return
        if isinstance(value, int) and value > 0:
            self._framerate_auto_check.setChecked(False)
            self._fps_spin.setValue(value)

    def _on_quality_changed(self, _index: int) -> None:
        """Reveal the Custom bitrate row only when Quality == Custom, and
        keep the description label in sync so the user sees what each
        preset actually means."""
        data = self._quality_combo.currentData()
        is_custom = data == "custom"
        self._bitrate_row_label.setVisible(is_custom)
        self._bitrate_spin.setVisible(is_custom)
        self._quality_desc_label.setText(_QUALITY_DESCRIPTIONS.get(data, ""))

    def _on_framerate_auto_toggled(self, checked: bool) -> None:
        """Keep the FPS spinner in sync with what's actually being used.

        When auto-match is on, the spinner mirrors the detected refresh
        rate so the user isn't staring at a stale manual value that
        contradicts what the recorder will do.
        """
        self._fps_spin.setDisabled(checked)
        if checked:
            self._fps_spin.setValue(self._detected_refresh_rate)

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox("Output")
        layout = QFormLayout(box)

        # Folder + browse + open
        self._output_edit = QLineEdit()
        # Per-keystroke refresh, debounced — ``shutil.disk_usage`` can
        # block hundreds of ms on UNC paths, so coalesce bursts.
        self._disk_hint_debounce = QTimer(self)
        self._disk_hint_debounce.setSingleShot(True)
        self._disk_hint_debounce.setInterval(200)
        self._disk_hint_debounce.timeout.connect(self._refresh_disk_free_hint)
        self._output_edit.textChanged.connect(self._disk_hint_debounce.start)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse_output)
        open_btn = QPushButton("Open folder")
        open_btn.setToolTip("Open the current recordings folder in Explorer.")
        open_btn.clicked.connect(self._on_open_output_folder)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._output_edit, stretch=1)
        row.addWidget(browse)
        row.addWidget(open_btn)
        wrap = QWidget()
        wrap.setLayout(row)
        layout.addRow("Folder:", wrap)

        # Free-disk-space hint, e.g. "1.2 TB free on C:". Refreshed on
        # Browse + editingFinished + when the panel reloads from config.
        self._disk_free_hint = _hint_label("")
        layout.addRow("", self._disk_free_hint)

        # Max storage — Momento deletes the oldest recordings when this is
        # exceeded. Combo of common presets + a Custom… escape hatch reveals
        # a spinner row when picked.
        self._max_storage_combo = AnchoredComboBox()
        for label, value in _MAX_STORAGE_PRESETS:
            self._max_storage_combo.addItem(label, value)
        self._max_storage_combo.setToolTip(
            "When the limit is reached, Momento deletes the oldest "
            "recordings first. Clips are always kept."
        )
        layout.addRow("Max storage:", self._max_storage_combo)
        self._max_storage_custom_label = QLabel("Custom (GB):")
        self._max_storage_custom_spin = QSpinBox()
        self._max_storage_custom_spin.setRange(1, 100_000)
        self._max_storage_custom_spin.setSuffix(" GB")
        self._max_storage_custom_spin.setMinimumWidth(160)
        layout.addRow(self._max_storage_custom_label, self._max_storage_custom_spin)
        _wire_preset_custom_row(
            self._max_storage_combo,
            self._max_storage_custom_label,
            self._max_storage_custom_spin,
        )
        layout.addRow("", _hint_label(
            "When the limit is reached, Momento deletes the oldest "
            "recordings first. Clips are kept."
        ))

        # Low-disk warning watermark — same preset+custom pattern.
        self._low_disk_combo = AnchoredComboBox()
        for label, value in _LOW_DISK_PRESETS:
            self._low_disk_combo.addItem(label, value)
        self._low_disk_combo.setToolTip(
            "Show a warning notification on Momento startup when the output "
            "drive has less free space than this."
        )
        layout.addRow("Low-disk warning at:", self._low_disk_combo)
        self._low_disk_custom_label = QLabel("Custom (GB):")
        self._low_disk_custom_spin = QSpinBox()
        self._low_disk_custom_spin.setRange(1, 1024)
        self._low_disk_custom_spin.setSuffix(" GB")
        self._low_disk_custom_spin.setMinimumWidth(160)
        layout.addRow(self._low_disk_custom_label, self._low_disk_custom_spin)
        _wire_preset_custom_row(
            self._low_disk_combo,
            self._low_disk_custom_label,
            self._low_disk_custom_spin,
        )

        return box

    def _refresh_disk_free_hint(self) -> None:
        text = self._output_edit.text().strip()
        hint = ""
        if text:
            path = Path(text).expanduser()
            free = free_bytes_for(path)
            if free is not None:
                drive = path.drive or str(path)
                hint = f"{format_bytes(free)} free on {drive}"
        self._disk_free_hint.setText(hint)

    def _on_open_output_folder(self) -> None:
        target = self._output_edit.text().strip() or str(self._config.output_folder)
        path = Path(target).expanduser()
        if not path.is_dir():
            QMessageBox.information(
                self, "Momento",
                "That folder doesn't exist yet — save the settings first so "
                "Momento can create it.",
            )
            return
        import os
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as e:
            QMessageBox.warning(self, "Momento", f"Couldn't open the folder:\n{e}")

    # ----------------------------------------------------------- YouTube
    def _build_youtube_account_group(self) -> QGroupBox:
        box = QGroupBox("Account")
        layout = QVBoxLayout(box)

        # Status line: "Signed in as: X" or "Not connected".
        self._yt_status_label = QLabel("")
        self._yt_status_label.setWordWrap(True)
        self._yt_status_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._yt_status_label)

        # Three buttons in one row: Connect / Switch / Disconnect.
        # Visibility flips based on connection state so the user only sees
        # the actions that apply right now.
        btn_row = QHBoxLayout()
        self._yt_connect_btn = QPushButton("Connect YouTube account…")
        self._yt_connect_btn.clicked.connect(self._on_yt_connect_clicked)
        self._yt_switch_btn = QPushButton("Switch account…")
        self._yt_switch_btn.clicked.connect(self._on_yt_connect_clicked)
        self._yt_disconnect_btn = QPushButton("Disconnect")
        self._yt_disconnect_btn.clicked.connect(self._on_yt_disconnect_clicked)
        btn_row.addWidget(self._yt_connect_btn)
        btn_row.addWidget(self._yt_switch_btn)
        btn_row.addWidget(self._yt_disconnect_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        layout.addWidget(_hint_label(
            "Momento uses Google's standard OAuth desktop flow — sign-in "
            "happens in your browser, on Google's site. Momento never sees "
            "your password. The sign-in token is encrypted on disk via "
            "Windows DPAPI, bound to your Windows account."
        ))
        return box

    def _build_youtube_defaults_group(self) -> QGroupBox:
        box = QGroupBox("Upload defaults")
        layout = QFormLayout(box)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._yt_privacy_combo = AnchoredComboBox()
        for value, label in (
            ("public", "Public — anyone can find it"),
            ("unlisted", "Unlisted — link-only sharing (recommended)"),
            ("private", "Private — only you can see it"),
        ):
            self._yt_privacy_combo.addItem(label, userData=value)
        layout.addRow("Default privacy", self._yt_privacy_combo)

        self._yt_category_combo = AnchoredComboBox()
        for cat_id, label in (
            (20, "Gaming"),
            (24, "Entertainment"),
            (23, "Comedy"),
            (22, "People & Blogs"),
            (17, "Sports"),
            (10, "Music"),
            (1, "Film & Animation"),
            (27, "Education"),
            (28, "Science & Technology"),
        ):
            self._yt_category_combo.addItem(label, userData=cat_id)
        layout.addRow("Default category", self._yt_category_combo)

        self._yt_default_tags_edit = QLineEdit()
        self._yt_default_tags_edit.setPlaceholderText("comma, separated, tags")
        layout.addRow("Default tags", self._yt_default_tags_edit)

        hint = _hint_label(
            "These defaults pre-fill the upload dialog — you can still override "
            "any of them per upload."
        )
        layout.addRow("", hint)
        return box

    # ---- YouTube connect/disconnect handlers (worker-thread blocking) ----

    def _on_yt_connect_clicked(self) -> None:
        """Spawn a worker thread to run the blocking OAuth flow.

        We can't call ``connect_account()`` directly on the GUI thread —
        ``run_local_server`` blocks until the user finishes the browser
        consent, which can take a minute and would freeze the Settings UI.
        """
        self._yt_set_busy(True, "Opening your browser to sign in to YouTube…")

        self._yt_thread = QThread(self)
        self._yt_worker = _YouTubeConnectWorker()
        self._yt_worker.moveToThread(self._yt_thread)
        self._yt_thread.started.connect(self._yt_worker.run)
        self._yt_worker.succeeded.connect(self._on_yt_connect_succeeded)
        self._yt_worker.failed.connect(self._on_yt_connect_failed)
        self._yt_worker.succeeded.connect(self._yt_thread.quit)
        self._yt_worker.failed.connect(self._yt_thread.quit)
        self._yt_thread.finished.connect(self._yt_worker.deleteLater)
        self._yt_thread.start()

    def _on_yt_connect_succeeded(self, info: object) -> None:
        # ChannelInfo dataclass; touch via attribute access.
        name = getattr(info, "name", "") or ""
        channel_id = getattr(info, "id", "") or ""
        self._config = replace(
            self._config,
            youtube_channel_name=name,
            youtube_channel_id=channel_id,
        )
        try:
            save_config(self._config)
        except OSError:
            logger.exception("Could not persist YouTube channel info after connect")
        # Crucially: tell the editor about the new config too — otherwise its
        # cached copy stays stale and the next time Settings is opened it
        # replays that stale copy via reload_from_config(), wiping the
        # displayed channel name back to "(unnamed channel)".
        self.settings_saved.emit(self._config)
        self._yt_set_busy(False)
        self._update_yt_status_label()
        QMessageBox.information(
            self,
            "Connected",
            f"Connected as: {name or '(unnamed channel)'}",
        )

    def _on_yt_connect_failed(self, message: str) -> None:
        self._yt_set_busy(False)
        self._update_yt_status_label()
        QMessageBox.warning(self, "YouTube sign-in failed", message)

    def _on_yt_disconnect_clicked(self) -> None:
        reply = QMessageBox.question(
            self,
            "Disconnect YouTube account",
            "Sign out of YouTube on this machine?\n\n"
            "The local sign-in token will be deleted. You can reconnect any time.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Lazy import — keeps the google libs out of the cold-start path.
        from momento.youtube import auth as yt_auth

        yt_auth.disconnect_account()
        self._config = replace(
            self._config, youtube_channel_name="", youtube_channel_id=""
        )
        try:
            save_config(self._config)
        except OSError:
            logger.exception("Could not persist YouTube disconnect")
        # Same reason as connect: keep the editor's cached config in sync so
        # the next Settings open doesn't replay a stale "still connected"
        # config.
        self.settings_saved.emit(self._config)
        self._update_yt_status_label()

    def _yt_set_busy(self, busy: bool, status: str = "") -> None:
        """Toggle button-disabled state during the OAuth flow."""
        for btn in (self._yt_connect_btn, self._yt_switch_btn, self._yt_disconnect_btn):
            btn.setEnabled(not busy)
        if busy and status:
            self._yt_status_label.setText(
                f"<span style='color:#aaa'><i>{status}</i></span>"
            )

    def _update_yt_status_label(self) -> None:
        """Repaint the connection state + button visibility."""
        from momento.youtube import auth as yt_auth

        connected = yt_auth.is_connected()
        name = self._config.youtube_channel_name
        if connected:
            display = name or "(unnamed channel)"
            self._yt_status_label.setText(
                f"<b>Signed in as:</b> {display}"
            )
            self._yt_connect_btn.setVisible(False)
            self._yt_switch_btn.setVisible(True)
            self._yt_disconnect_btn.setVisible(True)
        else:
            self._yt_status_label.setText(
                "<span style='color:#aaa'>Not connected. "
                "Sign in to enable Upload to YouTube on recordings and clips.</span>"
            )
            self._yt_connect_btn.setVisible(True)
            self._yt_switch_btn.setVisible(False)
            self._yt_disconnect_btn.setVisible(False)

    def _build_startup_group(self) -> QGroupBox:
        box = QGroupBox("Startup")
        layout = QVBoxLayout(box)

        self._autostart_check = QCheckBox("Start Momento with Windows")
        self._autostart_check.setToolTip(
            "Adds Momento to your user-level Run registry key — equivalent to "
            "dropping it in shell:startup."
        )
        layout.addWidget(self._autostart_check)

        self._monitor_on_launch_check = QCheckBox(
            "Begin monitoring games on launch"
        )
        self._monitor_on_launch_check.setToolTip(
            "If off, Momento sits in the tray without watching for games "
            "until you click “Resume monitoring” in the tray menu."
        )
        layout.addWidget(self._monitor_on_launch_check)

        self._close_to_tray_check = QCheckBox(
            "Close button minimises to tray"
        )
        self._close_to_tray_check.setToolTip(
            "Closing the editor window hides it instead of quitting the app. "
            "Use the tray's Quit menu item to fully exit."
        )
        layout.addWidget(self._close_to_tray_check)

        layout.addWidget(_hint_label(
            "When enabled, Momento starts in the system tray without "
            "opening the main window."
        ))
        return box

    def _build_notifications_group(self) -> QGroupBox:
        box = QGroupBox("Notifications")
        layout = QVBoxLayout(box)

        self._toast_started_check = QCheckBox(
            "Game detected — show “Recording started”"
        )
        self._toast_started_check.setToolTip(
            "Brief confirmation that Momento is now capturing — useful so you "
            "know you don't need to start anything by hand."
        )
        layout.addWidget(self._toast_started_check)

        self._toast_saved_check = QCheckBox(
            "Show “Recording saved” when a game exits"
        )
        self._toast_saved_check.setToolTip(
            "Confirms the clip was finalised on disk. Some users prefer to "
            "silence this since it tends to land in the middle of an "
            "end-of-game celebration."
        )
        layout.addWidget(self._toast_saved_check)

        self._toast_bookmark_check = QCheckBox(
            "Show “Bookmark added” when the hotkey lands"
        )
        self._toast_bookmark_check.setToolTip(
            "Orange overlay confirming the bookmark was recorded. The chime "
            "below is separate so you can keep one without the other."
        )
        layout.addWidget(self._toast_bookmark_check)

        self._toast_failure_check = QCheckBox(
            "Show “Couldn't record” when recording fails to start"
        )
        self._toast_failure_check.setToolTip(
            "Failures usually require action (missing mic, output folder "
            "gone) — recommended to keep this on."
        )
        layout.addWidget(self._toast_failure_check)

        # Position picker — drives RecordingToast._reposition().
        position_row = QHBoxLayout()
        position_row.setContentsMargins(0, 6, 0, 0)
        position_label = QLabel("Notification position:")
        position_label.setStyleSheet("color: #b8c1d1;")
        position_row.addWidget(position_label)
        self._notification_position_combo = AnchoredComboBox()
        for label, value in (
            ("Top-left", "top-left"),
            ("Top-right", "top-right"),
            ("Bottom-left", "bottom-left"),
            ("Bottom-right", "bottom-right"),
        ):
            self._notification_position_combo.addItem(label, value)
        position_row.addWidget(self._notification_position_combo)
        position_row.addStretch(1)
        layout.addLayout(position_row)

        layout.addWidget(_hint_label(
            "Notifications appear over the game window. Click any "
            "notification to dismiss it."
        ))
        return box

    def _build_bookmark_group(self) -> QGroupBox:
        box = QGroupBox("Bookmarks")
        layout = QFormLayout(box)
        self._bookmark_hotkey_edit = QLineEdit()
        self._bookmark_hotkey_edit.setPlaceholderText("F8")
        self._bookmark_hotkey_edit.setMaximumWidth(220)
        self._bookmark_hotkey_edit.setToolTip(
            "Examples: F8, F12, Ctrl+B, Ctrl+Shift+M. Active only while a "
            "recording is running."
        )
        layout.addRow("Hotkey:", self._bookmark_hotkey_edit)
        layout.addRow("", _hint_label(
            "Press this while recording to mark a moment on the timeline. "
            "Bookmarks show up as orange ticks and as clickable chips in the "
            "editor."
        ))

        self._bookmark_sound_check = QCheckBox("Play a soft chime when a bookmark is added")
        self._bookmark_sound_check.setToolTip(
            "Plays through your default speakers, so it lands in the "
            "recording too (handy as an audible marker; turn off if you'd "
            "rather not have beeps in your clips)."
        )
        layout.addRow("", self._bookmark_sound_check)
        return box

    def _build_games_group(self) -> QGroupBox:
        box = QGroupBox("Known games")
        layout = QVBoxLayout(box)

        self._fullscreen_check = QCheckBox(
            "Record fullscreen apps not in the game list"
        )
        self._fullscreen_check.setToolTip(
            "Catches games that aren't in the list. Momento skips a curated "
            "block-list of non-games (browsers, OBS, Parsec, VLC, IDEs, "
            "Office, Discord, …) and only fires when the same app is "
            "fullscreen for ~2 seconds, but mis-detection is still possible."
        )
        layout.addWidget(self._fullscreen_check)
        layout.addWidget(_hint_label(
            "May record non-game apps by mistake."
        ))

        # Search + filter row above the table.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 4, 0, 0)
        self._games_search_edit = QLineEdit()
        self._games_search_edit.setPlaceholderText("Search games…")
        self._games_search_edit.setClearButtonEnabled(True)
        self._games_search_edit.textChanged.connect(self._apply_games_filter)
        search_row.addWidget(self._games_search_edit, stretch=1)
        self._games_filter_combo = AnchoredComboBox()
        for label, value in (
            ("All games", "all"),
            ("Auto-record on", "enabled"),
            ("Auto-record off", "disabled"),
        ):
            self._games_filter_combo.addItem(label, value)
        self._games_filter_combo.currentIndexChanged.connect(self._apply_games_filter)
        search_row.addWidget(self._games_filter_combo)
        layout.addLayout(search_row)

        # Table of known games.
        self._games_table = QTableWidget(0, 3)
        self._games_table.setHorizontalHeaderLabels(
            ["Game", "Executable", "Auto-record"]
        )
        self._games_table.verticalHeader().setVisible(False)
        self._games_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._games_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._games_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._games_table.setSortingEnabled(True)
        h = self._games_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        # ResizeToContents picks the header label width (≈ 90 px) which clips
        # the pill horizontally; the pill needs ~70 px of body + side
        # padding so 110 px gives it room with margin to spare.
        self._games_table.setColumnWidth(2, 110)
        # Pill is 24 px tall; row needs 38 px so it has ≥ 6 px clearance
        # on each side after the grid line. Locking the vertical header
        # to Fixed prevents Qt from auto-shrinking rows to the tiny
        # QTableWidgetItem height when sorting fires.
        vh = self._games_table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(38)
        self._games_table.setMinimumHeight(260)
        layout.addWidget(self._games_table, stretch=1)
        layout.addWidget(_hint_label(
            f"Tick the box to auto-record that game. Momento ships a curated "
            f"list of {len(DEFAULT_KNOWN_GAMES)} popular titles."
        ))

        # Single row with a stretch separator. Left cluster: per-row
        # actions. Right cluster: list-maintenance actions. The wider
        # Games-tab cap (1280 px) gives them room without truncation.
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        add_btn = QPushButton("Add game…")
        add_btn.setToolTip("Add a game by typing its executable filename.")
        add_btn.clicked.connect(self._on_add_game_manually)
        btn_row.addWidget(add_btn)
        scan_btn = QPushButton("Scan running apps…")
        scan_btn.setToolTip(
            "List the executables currently running on your machine; pick "
            "the ones that are games."
        )
        scan_btn.clicked.connect(self._on_scan_running_apps)
        btn_row.addWidget(scan_btn)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._on_remove_selected_games)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        restore_btn = QPushButton("Restore defaults (merge)")
        restore_btn.setToolTip(
            "Add every entry from Momento's bundled list, keeping yours."
        )
        restore_btn.clicked.connect(self._on_restore_default_games)
        btn_row.addWidget(restore_btn)
        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._on_import_games)
        btn_row.addWidget(import_btn)
        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self._on_export_games)
        btn_row.addWidget(export_btn)
        layout.addLayout(btn_row)

        # Belt-and-braces minimum width so "Restore defaults (merge)" can
        # never get clipped to "estore defaults" if the user narrows the
        # window below the page max.
        for btn in (add_btn, scan_btn, remove_btn, restore_btn, import_btn, export_btn):
            btn.setMinimumWidth(btn.fontMetrics().horizontalAdvance(btn.text()) + 32)

        return box

    # ----------------------------------------------------------- games table
    def _load_games_table(self, known: list[str], disabled: list[str]) -> None:
        """Repopulate the games table from a (known, disabled) pair."""
        disabled_set = {g.lower() for g in disabled}
        table = self._games_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for exe in known:
            self._append_game_row(exe, enabled=exe.lower() not in disabled_set)
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)

    def _append_game_row(self, exe: str, *, enabled: bool) -> None:
        table = self._games_table
        row = table.rowCount()
        table.insertRow(row)
        # Each row needs an explicit height because the table's item-based
        # size calculation otherwise wins over our default-section-size
        # setting as soon as sorting / repaints fire.
        table.setRowHeight(row, 38)
        # Game name (read-only, derived from exe via humanise_game_name).
        name_item = QTableWidgetItem(humanise_game_name(exe))
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        # Store the exe on the name item too so sort+lookup is independent
        # of which column the user clicked.
        name_item.setData(Qt.ItemDataRole.UserRole, exe)
        exe_item = QTableWidgetItem(exe)
        exe_item.setFlags(exe_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        # Muted text on disabled rows so they read as paused.
        if not enabled:
            muted = QBrush(QColor("#6e7588"))
            italic = QFont()
            italic.setItalic(True)
            name_item.setForeground(muted)
            exe_item.setForeground(muted)
            name_item.setFont(italic)
            exe_item.setFont(italic)
        table.setItem(row, 0, name_item)
        table.setItem(row, 1, exe_item)
        pill = _make_onoff_pill(enabled)
        pill.toggled.connect(lambda checked, r=row: self._on_game_row_toggled(r, checked))
        wrap = QWidget()
        row_lay = QHBoxLayout(wrap)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(0)
        # Centre the pill in both axes; without AlignVCenter the layout
        # stretches the QPushButton to the cell height and the rounded
        # corners get clipped against the cell edges.
        row_lay.addStretch(1)
        row_lay.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        row_lay.addStretch(1)
        table.setCellWidget(row, 2, wrap)
        # Apply current search/filter to this fresh row so an Add-while-
        # filtering doesn't dump a hidden row into the user's selection.
        self._apply_games_filter_to_row(row)

    def _on_game_row_toggled(self, row: int, checked: bool) -> None:
        """Restyle the row + re-apply the filter when its auto-record state
        flips so the muted look + Auto-record on/off filter stay in sync."""
        table = self._games_table
        muted = QBrush(QColor("#6e7588"))
        normal = QBrush(QColor("#e6e8ee"))
        italic_font = QFont()
        italic_font.setItalic(not checked)
        for col in (0, 1):
            item = table.item(row, col)
            if item is None:
                continue
            item.setForeground(normal if checked else muted)
            item.setFont(italic_font)
        self._apply_games_filter_to_row(row)

    def _apply_games_filter(self) -> None:
        """Re-show/hide every row according to the current search + combo."""
        for row in range(self._games_table.rowCount()):
            self._apply_games_filter_to_row(row)

    def _apply_games_filter_to_row(self, row: int) -> None:
        table = self._games_table
        name_item = table.item(row, 0)
        exe_item = table.item(row, 1)
        pill = self._pill_for_row(row)
        if name_item is None or exe_item is None or pill is None:
            return
        needle = self._games_search_edit.text().strip().lower()
        if needle:
            haystack = f"{name_item.text()} {exe_item.text()}".lower()
            if needle not in haystack:
                table.setRowHidden(row, True)
                return
        mode = self._games_filter_combo.currentData() or "all"
        if mode == "enabled" and not pill.isChecked():
            table.setRowHidden(row, True)
            return
        if mode == "disabled" and pill.isChecked():
            table.setRowHidden(row, True)
            return
        table.setRowHidden(row, False)

    def _pill_for_row(self, row: int) -> QPushButton | None:
        """Return the auto-record pill for ``row``, or None if missing.

        Looks up by ``objectName`` so the cell widget's layout can change
        without breaking the table-collector logic.
        """
        wrap = self._games_table.cellWidget(row, 2)
        if wrap is None:
            return None
        return wrap.findChild(QPushButton, _ONOFF_PILL_OBJECT_NAME)

    def _collect_games_from_table(self) -> tuple[list[str], list[str]]:
        """Read the table and return (known, disabled). Order preserved by
        row order — which after sorting is by game-name alphabetical."""
        known: list[str] = []
        disabled: list[str] = []
        seen: set[str] = set()
        table = self._games_table
        for row in range(table.rowCount()):
            exe_item = table.item(row, 1)
            if exe_item is None:
                continue
            exe = exe_item.text().strip()
            if not exe or exe.lower() in seen:
                continue
            seen.add(exe.lower())
            known.append(exe)
            pill = self._pill_for_row(row)
            if pill is not None and not pill.isChecked():
                disabled.append(exe)
        return known, disabled

    def _add_game_if_new(self, exe: str) -> bool:
        """Append ``exe`` to the table if not already present. Returns True
        if added."""
        exe = exe.strip()
        if not exe:
            return False
        # Some users will paste a path; reduce to bare filename.
        exe = Path(exe).name
        if not exe.lower().endswith(".exe"):
            exe = exe + ".exe"
        # Already present?
        for row in range(self._games_table.rowCount()):
            item = self._games_table.item(row, 1)
            if item is not None and item.text().lower() == exe.lower():
                return False
        self._games_table.setSortingEnabled(False)
        self._append_game_row(exe, enabled=True)
        self._games_table.setSortingEnabled(True)
        self._games_table.sortItems(0, Qt.SortOrder.AscendingOrder)
        return True

    # ----------------------------------------------------------- games actions
    def _on_add_game_manually(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Add game",
            "Executable filename (e.g. eldenring.exe):",
        )
        if not ok:
            return
        if not self._add_game_if_new(text):
            QMessageBox.information(
                self, "Momento",
                "That executable is already in the list (or the name was empty).",
            )

    def _on_scan_running_apps(self) -> None:
        try:
            import psutil
        except ImportError:
            QMessageBox.warning(self, "Momento", "psutil isn't available; cannot scan running apps.")
            return
        seen_exes: set[str] = set()
        existing = set()
        for row in range(self._games_table.rowCount()):
            item = self._games_table.item(row, 1)
            if item is not None:
                existing.add(item.text().lower())
        # Collect candidate exes: top-level (no parent of our own) user apps
        # with a visible-looking name. We deliberately don't try to be clever
        # — the user picks from a flat list.
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").strip()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if not name or not name.lower().endswith(".exe"):
                continue
            if name.lower() in existing:
                continue
            seen_exes.add(name)
        if not seen_exes:
            QMessageBox.information(
                self, "Momento",
                "No new executables found. Try launching the game first, then scan.",
            )
            return
        picked = _pick_from_list(
            self, "Add running apps as games",
            "Tick the executables that are games:",
            sorted(seen_exes, key=str.lower),
        )
        if not picked:
            return
        added = 0
        for exe in picked:
            if self._add_game_if_new(exe):
                added += 1
        if added:
            self._status_bar_say(f"Added {added} game(s) — click Save to keep.")

    def _on_remove_selected_games(self) -> None:
        rows = sorted({idx.row() for idx in self._games_table.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "Momento", "Select one or more rows first.")
            return
        for row in rows:
            self._games_table.removeRow(row)

    def _on_restore_default_games(self) -> None:
        known, disabled = self._collect_games_from_table()
        existing_lower = {g.lower() for g in known}
        added = 0
        for g in DEFAULT_KNOWN_GAMES:
            if g.lower() not in existing_lower:
                known.append(g)
                existing_lower.add(g.lower())
                added += 1
        self._load_games_table(known, disabled)
        QMessageBox.information(
            self, "Momento",
            f"Merged {added} new entries; list now has {len(known)} games. "
            "Click Save to keep the changes.",
        )

    def _on_import_games(self) -> None:
        start = str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Import games list", start, "JSON (*.json)"
        )
        if not path:
            return
        import json
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Momento", f"Could not read {Path(path).name}:\n{e}")
            return
        # Accept either ``[exe, ...]`` (bare list) or ``{"known": [...], "disabled": [...]}``.
        if isinstance(data, list):
            known = [str(x) for x in data]
            disabled: list[str] = []
        elif isinstance(data, dict):
            known = [str(x) for x in data.get("known", [])]
            disabled = [str(x) for x in data.get("disabled", [])]
        else:
            QMessageBox.warning(self, "Momento", "Unrecognised file format.")
            return
        self._load_games_table(known, disabled)

    def _on_export_games(self) -> None:
        start = str(Path.home() / "momento-games.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export games list", start, "JSON (*.json)"
        )
        if not path:
            return
        import json
        known, disabled = self._collect_games_from_table()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"known": known, "disabled": disabled},
                    fh, indent=2, ensure_ascii=False,
                )
        except OSError as e:
            QMessageBox.warning(self, "Momento", f"Could not write {Path(path).name}:\n{e}")

    def _status_bar_say(self, message: str) -> None:
        """Best-effort status feedback on the host window."""
        host = self.window()
        bar = host.statusBar() if hasattr(host, "statusBar") else None
        if bar is not None:
            bar.showMessage(message, 4000)

    # --------------------------------------------------------------- helpers
    def _populate_devices(self) -> None:
        try:
            self._mic_devices = list_mic_devices()
        except Exception:
            logger.exception("Failed to enumerate WASAPI capture devices")
            self._mic_devices = []
        try:
            self._sys_devices = list_loopback_devices()
        except Exception:
            logger.exception("Failed to enumerate WASAPI playback endpoints")
            self._sys_devices = []

        prior_mic = (
            self._mic_combo.currentData()
            if self._mic_combo.count()
            else self._config.mic_device
        )
        prior_sys = (
            self._audio_combo.currentData()
            if self._audio_combo.count()
            else self._config.system_audio_device
        )

        # Mic dropdown: WASAPI capture endpoints. Same library as system
        # audio loopback, so the dropdown entries have consistent shape and
        # there's no more dshow-name-with-colons workaround needed.
        self._mic_combo.clear()
        self._mic_combo.addItem("— none —", "")
        for d in self._mic_devices:
            # Store the stable device id in userData; show the friendly name.
            self._mic_combo.addItem(d.name, d.id)

        # System audio dropdown: WASAPI playback endpoints (default first).
        # We store the *id* (stable across runs) in userData; the display name
        # may include "  (default)" suffix.
        self._audio_combo.clear()
        self._audio_combo.addItem("— none —", "")
        for d in self._sys_devices:
            self._audio_combo.addItem(d.name, d.id)

        _select_combo_by_text(self._mic_combo, prior_mic)
        _select_combo_by_text(self._audio_combo, prior_sys)
        self._refresh_device_status_labels()

    def _load_from_config(self) -> None:
        c = self._config
        _select_combo_by_text(self._mic_combo, c.mic_device)
        _select_combo_by_text(self._audio_combo, c.system_audio_device)
        self._refresh_device_status_labels()
        self._mic_vol_spin.setValue(c.mic_volume_pct)
        self._sys_vol_spin.setValue(c.system_volume_pct)
        # Spinbox shows the detected rate when auto is on so it always
        # reflects what the recorder will actually use (and not whatever
        # stale manual value was last saved).
        self._fps_spin.setValue(self._detected_refresh_rate if c.framerate_auto else c.framerate)
        self._framerate_auto_check.setChecked(c.framerate_auto)
        self._fps_spin.setDisabled(c.framerate_auto)
        # Sync the FPS preset combo to whichever box currently reflects the
        # config: auto → "Match", concrete value matches one of 30/60/120,
        # otherwise "Custom (use spinner)".
        if c.framerate_auto:
            preset_value: int = -1
        elif c.framerate in (30, 60, 120):
            preset_value = c.framerate
        else:
            preset_value = -2
        idx = self._fps_preset_combo.findData(preset_value)
        self._fps_preset_combo.blockSignals(True)
        self._fps_preset_combo.setCurrentIndex(max(0, idx))
        self._fps_preset_combo.blockSignals(False)
        # Custom row visible only when the preset is "Custom…".
        is_custom = preset_value == -2
        self._custom_fps_row_label.setVisible(is_custom)
        self._fps_spin.setVisible(is_custom)
        # Resolution + quality
        idx = self._resolution_combo.findData(c.target_resolution)
        self._resolution_combo.setCurrentIndex(max(0, idx))
        idx = self._quality_combo.findData(c.quality_preset)
        self._quality_combo.setCurrentIndex(max(0, idx))
        self._bitrate_spin.setValue(max(1_000, int(c.custom_bitrate_kbps)))
        self._output_edit.setText(str(c.output_folder))
        self._autostart_check.setChecked(c.autostart_with_windows)
        self._bookmark_hotkey_edit.setText(c.bookmark_hotkey)
        self._fullscreen_check.setChecked(c.record_any_fullscreen)
        self._toast_started_check.setChecked(c.show_recording_started_toast)
        self._toast_saved_check.setChecked(c.show_recording_saved_toast)
        self._toast_failure_check.setChecked(c.show_failure_toast)
        self._bookmark_sound_check.setChecked(c.bookmark_sound)
        self._toast_bookmark_check.setChecked(c.show_bookmark_toast)
        self._monitor_on_launch_check.setChecked(c.start_monitoring_on_launch)
        self._close_to_tray_check.setChecked(c.close_to_tray)
        # Select the notification-position option matching the saved value.
        idx = self._notification_position_combo.findData(c.notification_position)
        self._notification_position_combo.setCurrentIndex(max(0, idx))
        _load_preset_with_custom(
            self._max_storage_combo,
            self._max_storage_custom_spin,
            int(c.max_storage_gb),
            custom_default=50,
        )
        _load_preset_with_custom(
            self._low_disk_combo,
            self._low_disk_custom_spin,
            int(c.low_disk_warning_gb),
            custom_default=5,
        )
        self._refresh_disk_free_hint()
        self._load_games_table(c.known_games, c.disabled_games)
        # YouTube tab — defaults + connection state.
        idx = self._yt_privacy_combo.findData(c.youtube_default_privacy)
        self._yt_privacy_combo.setCurrentIndex(max(0, idx))
        idx = self._yt_category_combo.findData(int(c.youtube_default_category))
        self._yt_category_combo.setCurrentIndex(max(0, idx))
        self._yt_default_tags_edit.setText(c.youtube_default_tags)
        self._update_yt_status_label()

    def _on_browse_output(self) -> None:
        start = self._output_edit.text().strip() or str(Path.home())
        # The native Windows picker crashes in this app (likely COM/STA
        # conflict with the WGC + WASAPI background threads). Use Qt's own
        # picker, but pre-populate the sidebar with the standard Windows
        # locations the user expects — Home, Desktop, Documents, Downloads,
        # Videos, Pictures, plus the current output folder if it's set
        # somewhere unusual.
        dlg = QFileDialog(self.window(), "Choose output folder", start)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)

        sidebar: list[QUrl] = []
        seen: set[str] = set()
        for loc in (
            QStandardPaths.StandardLocation.HomeLocation,
            QStandardPaths.StandardLocation.DesktopLocation,
            QStandardPaths.StandardLocation.DocumentsLocation,
            QStandardPaths.StandardLocation.DownloadLocation,
            QStandardPaths.StandardLocation.MoviesLocation,
            QStandardPaths.StandardLocation.PicturesLocation,
        ):
            for p in QStandardPaths.standardLocations(loc):
                if p and p not in seen:
                    sidebar.append(QUrl.fromLocalFile(p))
                    seen.add(p)
        # Every mounted drive — without these the user can't navigate off
        # the C: profile tree, since Qt's non-native dialog has no "This PC"
        # shell root.
        for drive in logical_drives():
            p = str(drive)
            if p not in seen:
                sidebar.append(QUrl.fromLocalFile(p))
                seen.add(p)
        # Pin the current output folder too — quick "back where I was".
        cur = self._output_edit.text().strip()
        if cur and cur not in seen:
            sidebar.append(QUrl.fromLocalFile(cur))
        dlg.setSidebarUrls(sidebar)

        # Restore size from the last time the user opened this dialog; fall
        # back to a comfortably large default. Qt's non-native picker opens
        # tiny otherwise and forgets the size after every save.
        settings = QSettings(str(window_state_path()), QSettings.Format.IniFormat)
        geom = settings.value("dialogs/output_folder/geometry")
        if geom:
            dlg.restoreGeometry(geom)
        else:
            dlg.resize(900, 600)

        accepted = dlg.exec()
        settings.setValue("dialogs/output_folder/geometry", dlg.saveGeometry())
        if accepted:
            selected = dlg.selectedFiles()
            if selected:
                self._output_edit.setText(selected[0])
                self._refresh_disk_free_hint()


    def _maybe_migrate_recordings(self, new_folder: Path) -> bool:
        """If the output folder changed and the old location has recordings
        or clips, ask the user whether to move them. Returns False only when
        the user picks Cancel — i.e. the Save should abort.
        """
        old_folder = Path(self._config.output_folder).expanduser()
        try:
            if old_folder.resolve() == new_folder.resolve():
                return True
        except OSError:
            return True
        recordings, clips = count_movable(old_folder)
        if recordings == 0 and clips == 0:
            return True

        parts: list[str] = []
        if recordings:
            parts.append(f"{recordings} recording{'s' if recordings != 1 else ''}")
        if clips:
            parts.append(f"{clips} clip{'s' if clips != 1 else ''}")
        summary = " and ".join(parts)
        box = QMessageBox(self)
        box.setWindowTitle("Momento — move existing files?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"You have {summary} in:\n  {old_folder}\n\n"
            f"Move them to the new folder?\n  {new_folder}"
        )
        move_btn = box.addButton("Move", QMessageBox.ButtonRole.AcceptRole)
        leave_btn = box.addButton("Leave them", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(move_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is leave_btn:
            return True

        moved, failed = self._run_migration_with_progress(old_folder, new_folder)
        if failed:
            QMessageBox.warning(
                self, "Momento",
                f"Moved {moved} file(s); {failed} could not be moved "
                f"(in use, permission denied, or already present at the "
                f"destination). Check the log for details.",
            )
        return True

    def _run_migration_with_progress(
        self, old_folder: Path, new_folder: Path
    ) -> tuple[int, int]:
        """Run the migration on a worker thread driving a modal progress
        dialog. The pre-flight ``collect_media_pairs`` walk feeds both
        the dialog's max and the worker's iteration — no double scan."""
        worker = MigrationWorker(old_folder, new_folder)
        pairs = worker.collect_media_pairs()
        total = len(pairs)
        if total == 0:
            return 0, 0

        progress = QProgressDialog("Preparing…", "", 0, total, self)
        progress.setWindowTitle("Momento — Transferring files")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.setValue(0)

        thread = QThread(self)
        driver = _MigrationDriver(worker, pairs)
        driver.moveToThread(thread)

        result = {"moved": 0, "failed": 0}

        def _on_progress(done: int, total: int, name: str) -> None:
            progress.setMaximum(total)
            progress.setValue(done)
            if name:
                progress.setLabelText(f"Moving {name}\n({done + 1} of {total})")

        def _on_finished(moved: int, failed: int) -> None:
            result["moved"] = moved
            result["failed"] = failed
            progress.setValue(progress.maximum())
            progress.close()

        driver.progress_changed.connect(_on_progress)
        driver.finished.connect(_on_finished)
        thread.started.connect(driver.run)
        driver.finished.connect(thread.quit)
        driver.finished.connect(driver.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()
        progress.exec()  # modal — returns when _on_finished closes it

        return result["moved"], result["failed"]

    def _on_save(self) -> None:
        output_folder = Path(self._output_edit.text().strip()).expanduser()
        try:
            output_folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Momento", f"Cannot create output folder:\n{e}")
            return

        # Output-folder change: offer to move existing recordings + clips.
        # Cancel here aborts the whole Save so the user can rethink.
        if not self._maybe_migrate_recordings(output_folder):
            return

        mic = self._mic_combo.currentData() or ""
        sysaudio = self._audio_combo.currentData() or ""

        hotkey_spec = self._bookmark_hotkey_edit.text().strip() or "F8"
        try:
            parse_hotkey(hotkey_spec)  # validates without registering
        except HotkeyError as e:
            QMessageBox.warning(self, "Momento", f"Invalid bookmark hotkey:\n{e}")
            return

        known_games, disabled_games = self._collect_games_from_table()
        new_cfg = Config(
            mic_device=mic,
            system_audio_device=sysaudio,
            mic_volume_pct=int(self._mic_vol_spin.value()),
            system_volume_pct=int(self._sys_vol_spin.value()),
            output_folder=output_folder,
            autostart_with_windows=self._autostart_check.isChecked(),
            known_games=known_games,
            disabled_games=disabled_games,
            framerate=int(self._fps_spin.value()),
            framerate_auto=self._framerate_auto_check.isChecked(),
            bookmark_hotkey=hotkey_spec,
            record_any_fullscreen=self._fullscreen_check.isChecked(),
            show_recording_started_toast=self._toast_started_check.isChecked(),
            show_recording_saved_toast=self._toast_saved_check.isChecked(),
            show_failure_toast=self._toast_failure_check.isChecked(),
            # audio_offset_ms is preserved from the loaded config rather
            # than exposed in the UI. See _build_audio_group for rationale.
            audio_offset_ms=self._config.audio_offset_ms,
            bookmark_sound=self._bookmark_sound_check.isChecked(),
            show_bookmark_toast=self._toast_bookmark_check.isChecked(),
            notification_position=(
                self._notification_position_combo.currentData() or "top-left"
            ),
            close_to_tray=self._close_to_tray_check.isChecked(),
            start_monitoring_on_launch=self._monitor_on_launch_check.isChecked(),
            max_storage_gb=_read_preset_with_custom(
                self._max_storage_combo, self._max_storage_custom_spin
            ),
            low_disk_warning_gb=_read_preset_with_custom(
                self._low_disk_combo, self._low_disk_custom_spin
            ),
            target_resolution=self._resolution_combo.currentData() or "source",
            quality_preset=self._quality_combo.currentData() or "high",
            custom_bitrate_kbps=int(self._bitrate_spin.value()),
            youtube_default_privacy=(
                self._yt_privacy_combo.currentData() or "unlisted"
            ),
            youtube_default_category=int(
                self._yt_category_combo.currentData() or 20
            ),
            youtube_default_tags=self._yt_default_tags_edit.text().strip(),
            # channel_name / channel_id are managed by the Connect / Disconnect
            # buttons (which save_config directly) — preserve whatever's
            # currently in memory so a normal Save doesn't blow away an
            # active sign-in.
            youtube_channel_name=self._config.youtube_channel_name,
            youtube_channel_id=self._config.youtube_channel_id,
        )

        try:
            save_config(new_cfg)
        except OSError as e:
            QMessageBox.critical(self, "Momento", f"Could not save settings:\n{e}")
            return

        try:
            set_autostart(new_cfg.autostart_with_windows)
        except OSError as e:
            QMessageBox.warning(
                self, "Momento", f"Settings saved, but autostart change failed:\n{e}"
            )

        self._stop_mic_test()
        self.settings_saved.emit(new_cfg)
        self.done.emit()


# ---------------------------------------------------------------- helpers
def _select_combo_by_text(combo: QComboBox, text: str) -> None:
    """Select the item whose userData (or label) matches ``text``.

    Falls back to label match so older configs that stored a device's
    display name still resolve when the combo now stores stable device
    ids in userData. Once the user re-saves, the stored value migrates
    to the id form.
    """
    if not text:
        combo.setCurrentIndex(0)
        return
    for i in range(combo.count()):
        if combo.itemData(i) == text:
            combo.setCurrentIndex(i)
            return
    # Try label match (handles the legacy "name in config" case).
    for i in range(combo.count()):
        label = combo.itemText(i)
        bare = label.split("  (default)")[0]
        if label == text or bare == text:
            combo.setCurrentIndex(i)
            return
    # Configured device not present (unplugged): keep it as an extra entry so
    # the user can see what was previously selected.
    combo.addItem(f"{text}  (not detected)", text)
    combo.setCurrentIndex(combo.count() - 1)


class _YouTubeConnectWorker(QObject):
    """Run the blocking YouTube OAuth flow off the GUI thread.

    The auth flow opens the user's browser and waits for them to complete
    consent — could be 5 seconds or 5 minutes. Settings is built around a
    synchronous Qt event loop; running this inline would freeze the panel.

    Owned by ``SettingsPanel._on_yt_connect_clicked``. Emits exactly one
    of ``succeeded`` / ``failed`` on completion.
    """

    succeeded = pyqtSignal(object)   # auth.ChannelInfo
    failed = pyqtSignal(str)

    def run(self) -> None:  # noqa: D401 — Qt slot
        try:
            from momento.youtube import auth as yt_auth
            info = yt_auth.connect_account()
            self.succeeded.emit(info)
        except Exception as exc:  # noqa: BLE001 — surface anything to the UI
            logger.exception("YouTube connect worker failed")
            self.failed.emit(str(exc))


def _hint_label(text: str) -> QLabel:
    """Small grey wrapped helper-text label."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #888; font-size: 10pt;")
    return label


# Sentinel value used in storage / low-disk preset combos to mean "Custom…":
# when picked, the matching custom-row spinner is revealed and supplies the
# actual GB number.
_CUSTOM_PRESET_SENTINEL = -1

# Per-option captions for the Quality combo. Map mirrors the NVENC
# constant-quality (CQ) values in :mod:`momento.core.recorder`: lower CQ
# = higher quality = larger files.
_QUALITY_DESCRIPTIONS = {
    "low": (
        "Smallest files. Visible compression in fast motion — "
        "good for clip sharing where size matters most."
    ),
    "medium": (
        "Balanced size and quality. A safe pick if you're "
        "tight on disk."
    ),
    "high": (
        "Near-source quality with reasonable file sizes. "
        "Best for editing and archival — pick this unless you have a reason not to."
    ),
    "custom": (
        "You set the bitrate (kbit/s) in the row below. "
        "Use this only if you know what bitrate you want for your resolution."
    ),
}


# Common storage caps, matching the patterns Medal.tv / NVIDIA ShadowPlay
# expose. The last entry is the Custom… escape hatch.
_MAX_STORAGE_PRESETS: tuple[tuple[str, int], ...] = (
    ("Unlimited", 0),
    ("10 GB", 10),
    ("25 GB", 25),
    ("50 GB", 50),
    ("100 GB", 100),
    ("250 GB", 250),
    ("500 GB", 500),
    ("1 TB", 1024),
    ("Custom…", _CUSTOM_PRESET_SENTINEL),
)

_LOW_DISK_PRESETS: tuple[tuple[str, int], ...] = (
    ("Off", 0),
    ("5 GB", 5),
    ("10 GB", 10),
    ("25 GB", 25),
    ("50 GB", 50),
    ("100 GB", 100),
    ("Custom…", _CUSTOM_PRESET_SENTINEL),
)


def _load_preset_with_custom(
    combo: QComboBox,
    custom_spin: QSpinBox,
    value: int,
    *,
    custom_default: int,
) -> None:
    """Select ``value`` in ``combo`` if it's one of the presets; otherwise
    switch the combo to its Custom… row and load ``value`` into the
    spinner. The spinner is always populated with something sensible even
    when a preset matches, so flipping to Custom… post-hoc doesn't reveal
    an empty / surprising number."""
    idx = combo.findData(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)
        custom_spin.setValue(max(custom_spin.minimum(), value or custom_default))
        return
    # Not a preset — must land on Custom… and use the spinner.
    custom_idx = combo.findData(_CUSTOM_PRESET_SENTINEL)
    if custom_idx >= 0:
        combo.setCurrentIndex(custom_idx)
    custom_spin.setValue(max(custom_spin.minimum(), value))


def _read_preset_with_custom(combo: QComboBox, custom_spin: QSpinBox) -> int:
    """Inverse of :func:`_load_preset_with_custom` — returns the value the
    Config should store for this combo+spinner pair."""
    data = combo.currentData()
    if data == _CUSTOM_PRESET_SENTINEL:
        return int(custom_spin.value())
    return int(data) if data is not None else 0


def _wire_preset_custom_row(
    combo: QComboBox, custom_label: QLabel, custom_spin: QSpinBox
) -> None:
    """Hide ``custom_label`` + ``custom_spin`` unless the combo's selected
    item is the Custom… sentinel, and keep them in sync going forwards."""

    def _sync() -> None:
        is_custom = combo.currentData() == _CUSTOM_PRESET_SENTINEL
        custom_label.setVisible(is_custom)
        custom_spin.setVisible(is_custom)

    combo.currentIndexChanged.connect(lambda _i: _sync())
    _sync()


# Default max width for "form-like" tabs (Audio, Capture, Output, …) — keeps
# labels close to inputs on wide windows. Tabs that hold tables / wider
# tools pass their own max_width.
_DEFAULT_SETTINGS_WIDTH = 920
_GAMES_SETTINGS_WIDTH = 1280

# objectName on every auto-record pill so the games table can find them by
# name when collecting / filtering, regardless of layout shape.
_ONOFF_PILL_OBJECT_NAME = "GameAutoPill"

# Visual emphasis is inverted on purpose: "On" is the default, so it stays
# quiet (thin outline + muted text); "Off" pops with an amber fill so the
# user can spot paused games at a glance scrolling a long list.
_ONOFF_PILL_HEIGHT = 24  # px — fixed so the table cell doesn't stretch it
_ONOFF_PILL_WIDTH = 60

_ONOFF_PILL_STYLE = (
    "QPushButton#GameAutoPill { "
    "  padding: 0px 12px; border-radius: 12px; "
    "  background: transparent; color: #d4a64a; "
    "  border: 1px solid #6e5a2a; font-size: 9pt; "
    "  font-weight: 600; "
    "}"
    "QPushButton#GameAutoPill:hover { background: #2e2a1f; }"
    "QPushButton#GameAutoPill:checked { "
    "  background: transparent; color: #8a92a3; "
    "  border-color: #3a4150; font-weight: 500; "
    "}"
    "QPushButton#GameAutoPill:checked:hover { background: #262a33; }"
)


def _make_onoff_pill(checked: bool) -> QPushButton:
    """Build the auto-record pill — "On" when checked, "Off" otherwise.

    Locked to a fixed size so a parent QTableWidget cell can't stretch the
    button vertically past its border-radius (which produces the
    pancake-strip look). Styled via object-name so other QPushButtons in
    the same container aren't affected.
    """
    btn = QPushButton("On" if checked else "Off")
    btn.setObjectName(_ONOFF_PILL_OBJECT_NAME)
    btn.setCheckable(True)
    btn.setChecked(checked)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(_ONOFF_PILL_STYLE)
    btn.setFixedSize(_ONOFF_PILL_WIDTH, _ONOFF_PILL_HEIGHT)

    def _sync_label(on: bool) -> None:
        btn.setText("On" if on else "Off")

    btn.toggled.connect(_sync_label)
    return btn


def _action_button_row(on_save, on_cancel) -> QHBoxLayout:
    """Right-aligned Cancel + Save row, docked directly under a tab's content
    box so the actions sit with the controls they commit — at the box's
    bottom-right — instead of stranded at the window's bottom edge. Cancel
    discards and returns; Save persists and returns."""
    cancel_btn = QPushButton("Cancel")
    cancel_btn.clicked.connect(on_cancel)
    save_btn = QPushButton("Save")
    save_btn.setObjectName("primary")
    save_btn.setDefault(True)
    save_btn.setMinimumWidth(96)
    save_btn.clicked.connect(on_save)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    row.addStretch(1)
    row.addWidget(cancel_btn)
    row.addWidget(save_btn)
    return row


def _tab_with(
    content: QWidget,
    stretch_last: bool = False,
    *,
    max_width: int = _DEFAULT_SETTINGS_WIDTH,
    on_save=None,
    on_cancel=None,
) -> QWidget:
    """Wrap a group/widget inside a padded, width-capped tab page."""
    page = QWidget()
    outer = QHBoxLayout(page)
    outer.setContentsMargins(8, 6, 8, 6)
    outer.setSpacing(0)
    column = QWidget()
    column.setMaximumWidth(max_width)
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    if stretch_last:
        # Games tab — let the table inside the group expand vertically.
        layout.addWidget(content, stretch=1)
        if on_save is not None:
            layout.addLayout(_action_button_row(on_save, on_cancel))
    else:
        layout.addWidget(content)
        if on_save is not None:
            layout.addLayout(_action_button_row(on_save, on_cancel))
        layout.addStretch(1)
    outer.addWidget(column, stretch=1)
    outer.addStretch(0)  # right-hand gutter once column is at its max
    return page


def _tab_with_groups(
    *groups: QWidget, max_width: int = _DEFAULT_SETTINGS_WIDTH, on_save=None, on_cancel=None
) -> QWidget:
    """Wrap multiple groups in a width-capped tab page so the thinner tabs
    feel finished instead of having a small group floating in a tall empty
    rectangle."""
    page = QWidget()
    outer = QHBoxLayout(page)
    outer.setContentsMargins(8, 6, 8, 6)
    outer.setSpacing(0)
    column = QWidget()
    column.setMaximumWidth(max_width)
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for g in groups:
        layout.addWidget(g)
    if on_save is not None:
        layout.addLayout(_action_button_row(on_save, on_cancel))
    layout.addStretch(1)
    outer.addWidget(column, stretch=1)
    outer.addStretch(0)
    return page


def _tips_group(title: str, bullets: list[str]) -> QGroupBox:
    """Compact info card — bullet list of short hints. Used to round out
    pages whose primary controls are sparse."""
    box = QGroupBox(title)
    layout = QVBoxLayout(box)
    layout.setSpacing(6)
    for text in bullets:
        bullet = QLabel(f"•  {text}")
        bullet.setWordWrap(True)
        bullet.setStyleSheet("color: #b8c1d1; font-size: 10pt;")
        layout.addWidget(bullet)
    return box


def _pick_from_list(
    parent: QWidget, title: str, prompt: str, items: list[str]
) -> list[str]:
    """Modal multi-select dialog. Returns selected items, or [] on cancel."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(360, 420)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(prompt))
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    for s in items:
        item = QListWidgetItem(s)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        lst.addItem(item)
    lay.addWidget(lst, stretch=1)
    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    lay.addWidget(btns)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return []
    out: list[str] = []
    for i in range(lst.count()):
        item = lst.item(i)
        if item.checkState() == Qt.CheckState.Checked:
            out.append(item.text())
    return out


def _slider_with_spin(lo: int, hi: int, default: int) -> tuple[QSlider, QSpinBox, QWidget]:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(lo, hi)
    slider.setValue(default)
    slider.setTickInterval(50)
    slider.setTickPosition(QSlider.TickPosition.TicksBelow)

    spin = QSpinBox()
    spin.setRange(lo, hi)
    spin.setValue(default)
    spin.setSuffix(" %")
    spin.setMinimumWidth(80)

    slider.valueChanged.connect(spin.setValue)
    spin.valueChanged.connect(slider.setValue)

    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(slider, stretch=1)
    row.addWidget(spin)
    wrap = QWidget()
    wrap.setLayout(row)
    return slider, spin, wrap
