"""Microphone calibration uses the recording gain; no audio hardware is opened."""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import Config  # noqa: E402
from momento.core import mic_monitor as monitor_mod  # noqa: E402
from momento.core.mic_capture import MicDevice  # noqa: E402
from momento.ui import settings_dialog as settings_mod  # noqa: E402
from momento.ui import welcome as welcome_mod  # noqa: E402
from momento.ui.level_meter import LevelMeter  # noqa: E402


class AudioBoundary:
    """Feed PCM into the real worker and collect its actual playback bytes."""

    def __init__(self):
        self.input = queue.Queue()
        self.output = queue.Queue()
        self.closed = threading.Event()
        self.capture_closed = False
        self.playback_closed = False

    def read(self, _frames, exception_on_overflow):
        while not self.closed.is_set():
            try:
                return self.input.get(timeout=0.02)
            except queue.Empty:
                pass
        return np.zeros((4, 1), dtype=np.float32).tobytes()

    def write(self, raw):
        self.output.put(np.frombuffer(raw, dtype=np.float32).reshape(-1, 2))

    def stop_stream(self):
        pass

    def close(self):
        self.capture_closed = True
        self.playback_closed = True

    def feed(self, value):
        self.input.put(np.full((4, 1), value, dtype=np.float32).tobytes())
        return self.output.get(timeout=2)

    @contextlib.contextmanager
    def installed(self):
        device = MicDevice(name="Fixture microphone", id="fixture-mic")
        with (
            patch.object(monitor_mod, "resolve_mic_device", lambda _key: device),
            patch.object(monitor_mod.audio_devices, "pyaudio_session", lambda: contextlib.nullcontext(object())),
            patch.object(monitor_mod.audio_devices, "open_input_stream", lambda *_a, **_k: (self, 1, 48000)),
            patch.object(monitor_mod.audio_devices, "default_output_name", lambda _p: "fixture-output"),
            patch.object(monitor_mod.audio_devices, "open_output_stream", lambda *_a, **_k: (self, 2, 48000)),
        ):
            yield

    def stop(self, monitor):
        # Release the fake blocking read after stop() has signalled the real worker.
        timer = threading.Timer(0.02, self.closed.set)
        timer.start()
        monitor.stop()
        timer.join()


class CalibrationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @contextlib.contextmanager
    def settings_test(self, volume, *, start_test=True):
        boundary = AudioBoundary()
        with tempfile.TemporaryDirectory(prefix="mementos-mic-calibration-") as tmp:
            root = Path(tmp)
            device = MicDevice(name="Fixture microphone", id="fixture-mic")
            with (
                boundary.installed(),
                patch.object(settings_mod, "list_mic_devices", lambda: [device]),
                patch.object(settings_mod, "list_loopback_devices", lambda: []),
                patch("momento.util.paths.appdata_dir", lambda: root),
            ):
                panel = settings_mod.SettingsPanel(Config(
                    output_folder=root, mic_device=device.id, mic_volume_pct=volume,
                ))
                levels = []
                if start_test:
                    panel._mic_test_btn.setChecked(True)
                    panel._mic_monitor.level_changed.connect(levels.append)
                try:
                    yield panel, boundary, levels
                finally:
                    if panel._mic_monitor is not None:
                        boundary.stop(panel._mic_monitor)
                    self.app.processEvents()
                    panel.deleteLater()

    def wait_for_level(self, levels, count):
        deadline = time.monotonic() + 2
        while len(levels) < count and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertGreaterEqual(len(levels), count, "worker did not emit its measured level")
        return levels[count - 1]

    def test_saved_gain_reaches_both_audition_and_meter(self):
        with self.settings_test(150) as (_panel, boundary, levels):
            pcm = boundary.feed(0.2)
            np.testing.assert_allclose(pcm, 0.3, atol=1e-6)
            self.assertAlmostEqual(self.wait_for_level(levels, 1), 0.3, places=6)

    def test_live_slider_and_spin_changes_reach_audition_and_meter(self):
        with self.settings_test(100) as (panel, boundary, levels):
            panel._mic_slider.setValue(200)
            np.testing.assert_allclose(boundary.feed(0.2), 0.4, atol=1e-6)
            self.assertAlmostEqual(self.wait_for_level(levels, 1), 0.4, places=6)
            panel._mic_vol_spin.setValue(0)
            np.testing.assert_array_equal(boundary.feed(0.2), 0.0)
            self.assertEqual(self.wait_for_level(levels, 2), 0.0)

    def test_old_monitor_start_api_preserves_unity_gain(self):
        boundary = AudioBoundary()
        with boundary.installed():
            monitor = monitor_mod.MicMonitor()
            monitor.start("fixture-mic", monitor_to_speaker=True)
            try:
                np.testing.assert_allclose(boundary.feed(-0.2), -0.2, atol=1e-6)
            finally:
                boundary.stop(monitor)

    def test_live_mute_rechecks_signal_and_unmute_detects_again(self):
        with self.settings_test(100) as (panel, boundary, levels):
            boundary.feed(0.2)
            self.wait_for_level(levels, 1)
            self.assertEqual(panel._mic_status_caption.text(), "Signal detected")
            # Leave one old-gain peak queued for the GUI, as happens when a
            # slider event arrives before the next worker notification.
            boundary.feed(0.2)
            panel._mic_vol_spin.setValue(0)
            self.assertEqual(panel._mic_status_caption.text(), "Listening…")
            self.assertTrue(panel._mic_silence_timer.isActive())
            np.testing.assert_array_equal(boundary.feed(0.2), 0.0)
            self.assertEqual(self.wait_for_level(levels, 3), 0.0)
            panel._on_mic_silence()
            self.assertEqual(panel._mic_status_caption.text(), "Very low or no signal")

            panel._mic_slider.setValue(100)
            self.assertEqual(panel._mic_status_caption.text(), "Listening…")
            np.testing.assert_allclose(boundary.feed(0.2), 0.2, atol=1e-6)
            self.assertAlmostEqual(self.wait_for_level(levels, 4), 0.2, places=6)
            self.assertEqual(panel._mic_status_caption.text(), "Signal detected")
            self.assertFalse(panel._mic_silence_timer.isActive())

    def test_inactive_volume_changes_do_not_start_signal_detection(self):
        with self.settings_test(150, start_test=False) as (panel, _boundary, _levels):
            self.assertEqual(panel._mic_status_caption.text(), "")
            self.assertFalse(panel._mic_silence_timer.isActive())
            panel._mic_vol_spin.setValue(0)
            self.assertEqual(panel._mic_status_caption.text(), "")
            self.assertFalse(panel._mic_silence_timer.isActive())
            self.assertIsNone(panel._mic_monitor)

    def test_overload_remains_visible_in_meter_and_audition(self):
        with self.settings_test(200) as (panel, boundary, levels):
            np.testing.assert_allclose(boundary.feed(0.75), 1.5, atol=1e-6)
            self.assertAlmostEqual(self.wait_for_level(levels, 1), 1.5, places=6)
            self.assertIn("3.5 dBFS", panel._mic_meter.toolTip())
            rendered = panel._mic_meter.grab().toImage()
            full = rendered.pixelColor(int(rendered.width() * 0.95), rendered.height() // 2)
            self.assertGreater(full.red(), full.green() + 50)

    def test_quiet_valid_input_is_not_reported_absent(self):
        with self.settings_test(100) as (panel, boundary, levels):
            boundary.feed(0.01)  # -40 dBFS, well below the old -26 dBFS cutoff.
            self.wait_for_level(levels, 1)
            panel._on_mic_silence()
            self.assertTrue(panel._mic_input_seen)
            self.assertNotIn("No input", panel._mic_status_caption.text())

    def test_silence_is_not_reported_as_a_detected_signal(self):
        with self.settings_test(100) as (panel, boundary, levels):
            boundary.feed(0.0)
            self.wait_for_level(levels, 1)
            panel._on_mic_silence()
            self.assertFalse(panel._mic_input_seen)
            self.assertNotEqual(panel._mic_status_caption.text(), "Signal detected")

    def test_welcome_accepts_the_same_quiet_signal(self):
        with (
            patch.object(welcome_mod, "list_mic_devices", lambda: []),
            patch.object(welcome_mod, "list_loopback_devices", lambda: []),
        ):
            dialog = welcome_mod.WelcomeDialog(Config())
            try:
                dialog._on_mic_level(0.01)
                self.assertTrue(dialog._mic_input_seen)
                self.assertNotIn("No input", dialog._mic_caption.text())
            finally:
                dialog.deleteLater()

    def test_meter_shows_quiet_audio_on_a_decibel_scale(self):
        meter = LevelMeter()
        try:
            meter.resize(305, 24)
            meter.set_level(0.01)
            rendered = meter.grab().toImage()
            filled = rendered.pixelColor(70, 12)
            empty = rendered.pixelColor(170, 12)
            self.assertGreater(filled.green(), filled.red() + 20)
            self.assertLess(empty.green(), 80)
            self.assertIn("-40.0 dBFS", meter.toolTip())
            meter.reset()
            self.assertNotIn("-40.0 dBFS", meter.toolTip())
        finally:
            meter.deleteLater()


if __name__ == "__main__":
    unittest.main()
