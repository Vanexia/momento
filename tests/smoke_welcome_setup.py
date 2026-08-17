"""Headless checks for first-run wizard persistence boundaries."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

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
    finally:
        welcome_mod.list_mic_devices = real_mics
        welcome_mod.list_loopback_devices = real_outputs
        welcome_mod._persist_setup_config = real_persist
        QMessageBox.question = real_question

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
