"""Regression checks for Settings save validation and folder migration ordering."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QPixmap  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMessageBox as RealMessageBox,
    QPushButton,
)

import momento.ui.settings_dialog as settings_mod  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.ui.settings_dialog import SettingsPanel  # noqa: E402
from momento.ui import theme  # noqa: E402


_app = QApplication.instance() or QApplication(sys.argv[:1])
_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


class _MoveMessageBox:
    """Minimal QMessageBox stand-in that always chooses Move."""

    Icon = RealMessageBox.Icon
    ButtonRole = RealMessageBox.ButtonRole
    StandardButton = RealMessageBox.StandardButton
    instances = 0
    warnings: list[str] = []

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).instances += 1
        self._move = object()
        self._leave = object()
        self._cancel = object()

    def setWindowTitle(self, _title: str) -> None:
        pass

    def setIcon(self, _icon) -> None:
        pass

    def setText(self, _text: str) -> None:
        pass

    def addButton(self, button, _role=None):
        if button == "Move":
            return self._move
        if button == "Leave them":
            return self._leave
        return self._cancel

    def setDefaultButton(self, _button) -> None:
        pass

    def exec(self) -> int:
        return 0

    def clickedButton(self):
        return self._move

    @classmethod
    def warning(cls, _parent, _title: str, text: str, *_args):
        cls.warnings.append(text)
        return RealMessageBox.StandardButton.Ok

    @classmethod
    def critical(cls, _parent, _title: str, text: str, *_args):
        cls.warnings.append(text)
        return RealMessageBox.StandardButton.Ok


def _reset_box() -> None:
    _MoveMessageBox.instances = 0
    _MoveMessageBox.warnings.clear()


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(colour: str) -> float:
        rgb = QColor(colour).getRgbF()[:3]
        channels = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in rgb
        ]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    lighter, darker = sorted(
        (luminance(foreground), luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_settings_save_") as d:
        tmp = Path(d)
        old = (tmp / "old").resolve()
        old.mkdir()
        (old / "Wow_2026-01-01_120000.mkv").write_bytes(b"media")
        new = (tmp / "new").resolve()
        panel = SettingsPanel(
            Config(
                output_folder=old,
                quality_preset="high",
                custom_bitrate_kbps=12_000,
            )
        )
        panel.resize(1100, 800)
        panel.show()
        _app.processEvents()
        minimum_width = panel.minimumSizeHint().width()
        print(f"MEASURE - settings minimum width: {minimum_width}px")
        check(
            "settings layout: minimum width fits a 1366px display",
            minimum_width <= 1200,
        )

        panel.open_tab("Games")
        _app.processEvents()
        action_labels = {
            "Add game…",
            "Scan running apps…",
            "Remove selected",
            "Restore defaults (merge)",
            "Import…",
            "Export…",
        }
        action_buttons = [
            button
            for button in panel.findChildren(QPushButton)
            if button.text() in action_labels
        ]
        action_rows = {button.geometry().top() for button in action_buttons}
        print(f"MEASURE - Games action rows at 1100px: {len(action_rows)}")
        check(
            "settings layout: Games actions reflow at constrained width",
            len(action_buttons) == len(action_labels) and len(action_rows) >= 2,
        )

        panel.open_tab("Audio")
        nav_item = panel._nav_items[1]
        before_focus = QPixmap(nav_item.size())
        nav_item.render(before_focus)
        nav_item.setFocus(Qt.FocusReason.TabFocusReason)
        _app.processEvents()
        after_focus = QPixmap(nav_item.size())
        nav_item.render(after_focus)
        check(
            "settings navigation: keyboard focus is accepted and visible",
            bool(nav_item.focusPolicy() & Qt.FocusPolicy.TabFocus)
            and nav_item.hasFocus()
            and before_focus.toImage() != after_focus.toImage(),
        )
        QTest.keyClick(nav_item, Qt.Key.Key_Return)
        _app.processEvents()
        check(
            "settings navigation: Enter activates the focused page",
            panel._stack.currentIndex() == 1,
        )

        panel.open_tab("Games")
        panel._games_table.setCurrentCell(0, 2)
        panel._games_table.setFocus(Qt.FocusReason.TabFocusReason)
        _app.processEvents()
        initial_auto_record = panel._row_enabled(0)
        QTest.keyClick(panel._games_table, Qt.Key.Key_Space)
        _app.processEvents()
        after_space = panel._row_enabled(0)
        QTest.keyClick(panel._games_table, Qt.Key.Key_Return)
        _app.processEvents()
        check(
            "settings Games: Space and Enter toggle auto-record",
            after_space is not initial_auto_record
            and panel._row_enabled(0) is initial_auto_record,
        )

        group_label_colour = getattr(
            theme, "SETTINGS_GROUP_LABEL", theme.TEXT_FAINT
        )
        group_label_contrast = _contrast_ratio(group_label_colour, theme.BG_CARD)
        print(f"MEASURE - settings group-label contrast: {group_label_contrast:.2f}:1")
        check(
            "settings theme: group labels meet WCAG AA contrast",
            group_label_contrast >= 4.5,
        )

        visible_copy = " ".join(
            label.text() for label in panel.findChildren(QLabel)
        ).lower()
        check(
            "capture tips: target presets disclose possible downscaling",
            "target presets may downscale" in visible_copy
            and "never upscales" in visible_copy,
        )
        check(
            "capture tips: interrupted MKVs are described as recoverable",
            "recoverable" in visible_copy and "still playable" not in visible_copy,
        )
        low_disk_tip = panel._low_disk_combo.toolTip().lower()
        check(
            "output low-disk copy: threshold, Off, and 1 GB floor are explicit",
            "stop" in low_disk_tip
            and "threshold" in low_disk_tip
            and "off (0 gb)" in low_disk_tip
            and "1 gb" in low_disk_tip,
        )

        high_index = panel._quality_combo.findData("high")
        custom_index = panel._quality_combo.findData("custom")
        check("capture quality: saved High selection is retained", panel._quality_combo.currentData() == "high")
        check("capture quality: High is labelled as best quality", "best quality" in panel._quality_combo.itemText(high_index).lower())
        check("capture quality: High explains uncapped storage use", (
            "10 GB per hour" in panel._quality_desc_label.text()
            and "not capped" in panel._quality_desc_label.text()
        ))
        check("capture quality: saved custom bitrate stays visible", (
            not panel._bitrate_spin.isHidden()
            and panel._bitrate_spin.value() == 12_000
        ))
        check("capture quality: custom bitrate is clearly inactive under High", (
            not panel._bitrate_spin.isEnabled()
            and "inactive" in panel._bitrate_row_label.text().lower()
        ))
        panel._quality_combo.setCurrentIndex(custom_index)
        check("capture quality: Custom enables the retained bitrate", (
            panel._bitrate_spin.isEnabled()
            and panel._bitrate_spin.value() == 12_000
            and panel._bitrate_row_label.text() == "Custom bitrate:"
        ))
        check("capture quality: changing the control does not save immediately", panel._config.quality_preset == "high")
        panel._quality_combo.setCurrentIndex(high_index)

        real_box = settings_mod.QMessageBox
        real_save = settings_mod.save_config
        real_autostart = settings_mod.set_autostart
        settings_mod.QMessageBox = _MoveMessageBox
        settings_mod.set_autostart = lambda _enabled: None
        try:
            # Validation must happen before even asking about (let alone doing)
            # a move. The old implementation moved first and rejected the save
            # only afterwards.
            _reset_box()
            events: list[str] = []
            panel._output_edit.setText(str(new))
            panel._bookmark_hotkey_edit.setText("Ctrl+")
            panel._run_migration_with_progress = lambda *_args: (events.append("migrate") or (1, 0))
            settings_mod.save_config = lambda _cfg: events.append("save")
            panel._on_save()
            check("invalid hotkey prevents the migration prompt", _MoveMessageBox.instances == 0)
            check("invalid hotkey causes no move and no save", events == [])

            # A blank field must not become Path('.') and silently redirect all
            # future recordings to the process working directory.
            _reset_box()
            events.clear()
            panel._output_edit.setText("   ")
            panel._bookmark_hotkey_edit.setText("F8")
            panel._on_save()
            check("blank output folder is rejected", bool(_MoveMessageBox.warnings))
            check("blank output folder is never persisted or migrated", events == [])

            # Cross-drive shutil.move copies before deleting. Moving the live
            # MKV could therefore create a truncated duplicate while the real
            # recording continues in the old folder.
            _reset_box()
            events.clear()
            panel._output_edit.setText(str(new))
            panel._session = SimpleNamespace(
                current_output=old / "Wow_2026-01-01_120000.mkv"
            )
            panel._on_save()
            check("output folder cannot change during a live recording", bool(_MoveMessageBox.warnings))
            check("live recording blocks both config save and migration", events == [])
            panel._session = None

            # The recording can begin while the Move / Leave / Cancel prompt is
            # open. Re-check after that prompt, immediately before persistence,
            # so the old live MKV is never included in a migration.
            _reset_box()
            events.clear()
            panel._session = SimpleNamespace(current_output=None)
            panel._output_edit.setText(str(new))

            def start_recording_during_prompt(_folder: Path) -> bool:
                panel._session.current_output = old / "started-during-prompt.mkv"
                events.append("prompt")
                return True

            panel._maybe_migrate_recordings = start_recording_during_prompt
            panel._run_migration_with_progress = lambda *_args: (
                events.append("migrate") or (1, 0)
            )
            settings_mod.save_config = lambda _cfg: events.append("save")
            panel._on_save()
            check(
                "recording started during migration prompt aborts folder change",
                events == ["prompt"],
            )
            check(
                "prompt race explains why settings were not changed",
                any("current recording" in warning for warning in _MoveMessageBox.warnings),
            )
            panel._session = None

            # Restore the normal decision helper for the ordering checks below.
            panel._maybe_migrate_recordings = (
                lambda _folder: True
            )

            # A failed atomic config write must leave every existing recording
            # in the old folder. Otherwise the app still points at the old path
            # and the moved library appears to vanish.
            _reset_box()
            events.clear()
            panel._output_edit.setText(str(new))

            def fail_save(_cfg) -> None:
                events.append("save")
                raise OSError("simulated config write failure")

            settings_mod.save_config = fail_save
            panel._on_save()
            check("failed config save does not start migration", events == ["save"])

            # On success, persistence is the commit point and migration follows.
            _reset_box()
            events.clear()
            settings_mod.save_config = lambda _cfg: events.append("save")
            panel._on_save()
            check("successful save happens before migration", events == ["save", "migrate"])

            # Folder changes form a monitoring transaction: stop accepting new
            # games, drain a pending startup, commit + move, apply the new
            # config synchronously, and only then resume detection.
            _reset_box()
            events.clear()

            class MonitoringSession:
                def __init__(self, *, become_active_on_pause: bool = False) -> None:
                    self.current_output = None
                    self.is_monitoring = True
                    self._become_active_on_pause = become_active_on_pause

                def pause_monitoring(self) -> None:
                    events.append("pause")
                    self.is_monitoring = False
                    if self._become_active_on_pause:
                        self.current_output = old / "pending-start.mkv"

                def resume_monitoring(self) -> None:
                    events.append("resume")
                    self.is_monitoring = True

            panel._session = MonitoringSession()
            panel._output_edit.setText(str(new))
            panel._maybe_migrate_recordings = lambda _folder: True
            panel._run_migration_with_progress = lambda *_args: (
                events.append("migrate") or (1, 0)
            )
            settings_mod.save_config = lambda _cfg: events.append("save")

            def applied(_cfg) -> None:
                events.append("apply")

            panel.settings_saved.connect(applied)
            panel._on_save()
            panel.settings_saved.disconnect(applied)
            check(
                "folder migration pauses monitoring through config application",
                events == ["pause", "save", "migrate", "apply", "resume"],
            )

            # A start that was already entering the recorder when pause began
            # becomes visible after the starter is drained. Abort without
            # saving or moving, then restore the prior monitoring state.
            _reset_box()
            events.clear()
            panel._session = MonitoringSession(become_active_on_pause=True)
            panel._on_save()
            check(
                "folder migration aborts when pause exposes a pending recording",
                events == ["pause", "resume"],
            )
            check(
                "folder migration explains the drained-start abort",
                any("current recording" in warning for warning in _MoveMessageBox.warnings),
            )
            panel._session = None
        finally:
            settings_mod.QMessageBox = real_box
            settings_mod.save_config = real_save
            settings_mod.set_autostart = real_autostart
            panel.close()
            _app.processEvents()

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
