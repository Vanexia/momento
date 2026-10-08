"""Headless checks for first-run wizard persistence boundaries."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QObject, Qt, pyqtSignal  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox  # noqa: E402

from momento.config import Config  # noqa: E402
from momento.core.audio_loopback import LoopbackDevice  # noqa: E402
from momento.core.mic_capture import MicDevice  # noqa: E402
from momento.ui import welcome as welcome_mod  # noqa: E402


checks = 0
failures = 0


def check(label: str, condition: bool) -> None:
    global checks, failures
    checks += 1
    if condition:
        print(f"PASS - {label}")
    else:
        failures += 1
        print(f"FAIL - {label}")


class _MicMonitor(QObject):
    """Replace hardware I/O while exercising the wizard's real lifecycle."""

    level_changed = pyqtSignal(float)
    error = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_running = False
        self.device = ""

    def start(self, device, monitor_to_speaker):
        self.device = device
        self.is_running = True

    def stop(self):
        self.is_running = False
        self.stopped.emit()


class _Player(QObject):
    errorOccurred = pyqtSignal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.playing = False

    def setAudioOutput(self, output):
        self.output = output

    def setSource(self, source):
        self.source = source

    def play(self):
        self.playing = True

    def stop(self):
        self.playing = False


class _AudioOutput(QObject):
    def __init__(self, device, parent=None):
        super().__init__(parent)

    def setVolume(self, volume):
        pass


def check_wizard_flow(app: QApplication) -> None:
    """All filesystem writes stay in scratch; no audio or startup registration."""
    with tempfile.TemporaryDirectory(prefix="mementos-setup-flow-") as tmp:
        root = Path(tmp)
        recordings = root / "recordings"
        recordings.mkdir()
        chime = root / "chime.wav"
        chime.touch()
        cfg = Config(output_folder=recordings, mic_device="A", system_audio_device="S")
        original = cfg.to_dict()
        saved: list[Config] = []
        warnings: list[str] = []
        mics = [MicDevice(name="Mic A", id="A"), MicDevice(name="Mic B", id="B")]
        outputs = [LoopbackDevice(name="Speakers", id="S")]
        with (
            patch.object(welcome_mod, "list_mic_devices", lambda: mics),
            patch.object(welcome_mod, "list_loopback_devices", lambda: outputs),
            patch.object(welcome_mod, "MicMonitor", _MicMonitor),
            patch.object(welcome_mod, "QMediaPlayer", _Player),
            patch.object(welcome_mod, "QAudioOutput", _AudioOutput),
            patch.object(welcome_mod, "match_audio_output", lambda _id: object()),
            patch.object(welcome_mod, "bookmark_sound_path", lambda: chime),
            patch.object(welcome_mod, "_persist_setup_config", lambda value: saved.append(value)),
            patch.object(QMessageBox, "warning", lambda _p, _t, text: warnings.append(text)),
            patch.object(QMessageBox, "critical", lambda _p, _t, text: warnings.append(text)),
            patch("momento.util.paths.appdata_dir", lambda: root),
            patch("momento.util.windows_api.logical_drives", lambda: [Path("C:/"), Path("D:/")]),
        ):
            dialog = welcome_mod.WelcomeDialog(cfg)
            picker_state = {}

            def inspect_picker(picker):
                picker_state["sidebar"] = {Path(u.toLocalFile()) for u in picker.sidebarUrls()}
                picker_state["non_native"] = picker.testOption(QFileDialog.Option.DontUseNativeDialog)
                picker_state["width"] = picker.width()
                return 0

            with patch.object(QFileDialog, "exec", inspect_picker):
                dialog._on_browse_folder()
            check("folder picker: C and D drives are directly visible", {Path("C:/"), Path("D:/")} <= picker_state["sidebar"])
            check("folder picker: retains Qt picker crash workaround", picker_state["non_native"])
            check("folder picker: opens at a useful size", picker_state["width"] >= 800)
            check("folder picker: cancel preserves current choice", dialog._folder_edit.text() == str(recordings))

            for value, label in (("", "blank"), (".", "working directory"), (str(chime), "existing file")):
                dialog._stack.setCurrentIndex(1)
                dialog._folder_edit.setText(value)
                dialog._on_next()
                check(f"folder validation: Next rejects {label}", dialog._stack.currentIndex() == 1)
            dialog._stack.setCurrentIndex(7)
            dialog._folder_edit.clear()
            dialog._refresh_final_checklist()
            check("review: empty folder is not marked ready", "✓" not in dialog._final_labels[0].text())
            dialog._on_finish()
            check("finish: invalid folder returns to its step without saving", not saved and dialog._stack.currentIndex() == 1)

            dialog._folder_edit.setText(str(recordings))
            dialog._stack.setCurrentIndex(5)
            dialog._hotkey_edit.setText("banana")
            dialog._on_next()
            check("hotkey validation: invalid key blocks Next", dialog._stack.currentIndex() == 5)
            dialog._stack.setCurrentIndex(7)
            dialog._on_finish()
            check("finish: invalid hotkey returns to its step without saving", not saved and dialog._stack.currentIndex() == 5)

            dialog._hotkey_edit.setText("<b>F8</b>")
            dialog._refresh_final_checklist()
            summary = " ".join(label.text() for label in dialog._final_labels)
            check("review: user text cannot become HTML", "<b>F8</b>" not in summary and "&lt;b&gt;F8&lt;/b&gt;" in summary)
            dialog._hotkey_edit.setText("Ctrl+Shift+B")
            dialog._stack.setCurrentIndex(5)
            dialog._on_next()
            check("hotkey validation: valid combination advances", dialog._stack.currentIndex() == 6)
            check("navigation: choices remain unsaved until Finish", not saved and cfg.to_dict() == original)
            dialog.reject()
            check("cancel: choices never modify config", not saved and cfg.to_dict() == original)
            dialog.deleteLater()

            for exit_kind in ("escape", "reject", "accept", "close", "next", "refresh", "device"):
                dialog = welcome_mod.WelcomeDialog(cfg)
                dialog._stack.setCurrentIndex(2)
                dialog._mic_test_btn.setChecked(True)
                monitor = dialog._mic_monitor
                if exit_kind == "escape":
                    dialog.show()
                    app.processEvents()
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                elif exit_kind == "next":
                    dialog._on_next()
                elif exit_kind == "refresh":
                    dialog._reload_audio_devices()
                elif exit_kind == "device":
                    dialog._wizard_mic_combo.setCurrentIndex(2)
                else:
                    getattr(dialog, exit_kind)()
                check(f"microphone test: {exit_kind} stops capture and timer", not monitor.is_running and not dialog._mic_silence_timer.isActive())
                monitor.stop()
                dialog.deleteLater()

            dialog = welcome_mod.WelcomeDialog(cfg)
            dialog._stack.setCurrentIndex(2)
            dialog._on_sys_test_clicked()
            previous = dialog._sys_test_player[0]
            dialog._on_sys_test_clicked()
            check("system test: repeat stops the previous chime", not previous.playing)
            current = dialog._sys_test_player[0]
            dialog._on_next()
            check("system test: leaving Audio stops playback", not current.playing)
            dialog._stack.setCurrentIndex(2)
            dialog._on_sys_test_clicked()
            current = dialog._sys_test_player[0]
            dialog.reject()
            check("system test: dismissing setup stops playback", not current.playing)
            dialog.deleteLater()

            custom = replace(cfg, framerate=75, framerate_auto=False, custom_bitrate_kbps=24_000)
            dialog = welcome_mod.WelcomeDialog(custom)
            check("capture: existing custom FPS is visible", dialog._fps_combo.currentData() == 75)
            check("capture: existing custom bitrate is visible", "24" in dialog._quality_combo.currentText())
            dialog._quality_combo.setCurrentIndex(dialog._quality_combo.findData("high"))
            custom_index = next((i for i in range(dialog._quality_combo.count()) if "24" in dialog._quality_combo.itemText(i)), -1)
            if custom_index >= 0:
                dialog._quality_combo.setCurrentIndex(custom_index)
            dialog._on_finish()
            check("capture: keeping custom values saves exactly those values", bool(saved) and saved[-1].framerate == 75 and not saved[-1].framerate_auto and saved[-1].custom_bitrate_kbps == 24_000 and saved[-1].quality_preset == "custom")
            dialog._refresh_final_checklist()
            check("review: capture summary includes bitrate", "24 Mbit/s" in dialog._final_labels[3].text())
            dialog.deleteLater()

            long_mic = "Microphone (External USB Audio Interface with Headphone Monitoring and Voice Processing)"
            long_output = "Speakers (Digital Surround Sound Playback Device with External USB Audio Interface)"
            mics[:] = [MicDevice(name=long_mic, id="A")]
            outputs[:] = [LoopbackDevice(name=long_output, id="S")]
            dialog = welcome_mod.WelcomeDialog(replace(
                cfg, output_folder=Path("D:/Game recordings/World of Warcraft beta/Adventures with friends and guildmates/Long recording archive folder for testing the setup review")
            ))
            dialog._stack.setCurrentIndex(7)
            dialog.resize(560, 480)
            dialog.show()
            app.processEvents()
            check(
                "review: long paths and device names remain readable at minimum size",
                all(label.height() >= label.heightForWidth(label.width()) for label in dialog._final_labels),
            )
            check("review: Finish stays inside the minimum-size dialog", dialog._next_btn.geometry().bottom() < dialog.height())
            dialog.reject()
            dialog.deleteLater()
            app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    real_mics = welcome_mod.list_mic_devices
    real_outputs = welcome_mod.list_loopback_devices
    real_question = QMessageBox.question
    real_persist = welcome_mod._persist_setup_config
    saved: list[Config] = []
    try:
        welcome_mod.list_mic_devices = lambda: []
        welcome_mod.list_loopback_devices = lambda: []
        welcome_mod._persist_setup_config = lambda config: saved.append(config)
        QMessageBox.question = lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes

        cfg = Config(
            mic_device="Disconnected Microphone",
            system_audio_device="Disconnected Speakers",
        )
        dialog = welcome_mod.WelcomeDialog(cfg)
        check(
            "welcome layout: minimum height fits a 768px laptop display",
            dialog.minimumSizeHint().height() <= 680,
        )
        check(
            "missing devices: visible empty choices replace stale saved values",
            dialog._pending["mic_device"] == ""
            and dialog._pending["system_audio_device"] == "",
        )
        welcome_mod.list_mic_devices = lambda: [
            MicDevice(name="New Microphone", id="New Microphone")
        ]
        welcome_mod.list_loopback_devices = lambda: [
            LoopbackDevice(name="New Speakers", id="New Speakers")
        ]
        dialog._reload_audio_devices()
        check(
            "device refresh: newly connected devices appear without restarting",
            dialog._wizard_mic_combo.findData("New Microphone") >= 0
            and dialog._wizard_sys_combo.findData("New Speakers") >= 0,
        )
        dialog._wizard_mic_combo.setCurrentIndex(
            dialog._wizard_mic_combo.findData("New Microphone")
        )
        dialog._wizard_sys_combo.setCurrentIndex(
            dialog._wizard_sys_combo.findData("New Speakers")
        )
        check(
            "device status: enumeration is labelled Detected, not Connected",
            "Detected" in dialog._wizard_mic_status.text()
            and "Connected" not in dialog._wizard_mic_status.text(),
        )
        visible_copy = " ".join(
            label.text() for label in dialog.findChildren(QLabel)
        )
        check(
            "welcome privacy copy: chosen cloud-synced folders are not overpromised",
            "does not upload" in visible_copy
            and "cloud sync depends" in visible_copy,
        )
        check(
            "welcome storage copy: unlimited default and safety stop are explicit",
            "quota cleanup is off" in visible_copy
            and "low-space safety stop" in visible_copy,
        )
        dialog._wizard_mic_combo.setCurrentIndex(0)
        dialog._wizard_sys_combo.setCurrentIndex(0)

        with tempfile.TemporaryDirectory(prefix="momento-welcome-") as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                dialog._folder_edit.setText("recordings")
                dialog._on_finish()
            finally:
                os.chdir(previous_cwd)

        check("finish: setup config was persisted", len(saved) == 1)
        if saved:
            check("finish: output folder is absolute", saved[0].output_folder.is_absolute())
            check("finish: audio matches the visible empty choices", not saved[0].mic_device and not saved[0].system_audio_device)
            check("finish: explicit completion marker is set", saved[0].setup_complete)
        dialog.deleteLater()
        app.processEvents()
        check_wizard_flow(app)
    finally:
        welcome_mod.list_mic_devices = real_mics
        welcome_mod.list_loopback_devices = real_outputs
        welcome_mod._persist_setup_config = real_persist
        QMessageBox.question = real_question

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
