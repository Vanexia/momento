"""Live recording-status strip shown at the top of the editor window.

Surfaces what Momento is doing right now — whether it's recording, which
game, how long for — plus quick-glance environment checks (mic configured,
system audio configured, free disk space). Driven from the tray's
session-status signal and a 1 s QTimer that polls the recorder + filesystem.

Only the tray builds it; the editor exposes :py:meth:`EditorWindow.status_panel`
so the tray can call :py:meth:`StatusPanel.set_status` from its existing
``_apply_status`` slot.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from momento.config import Config
from momento.core.game_names import humanise_game_name
from momento.core.game_watcher import ActiveGame
from momento.core.session import STATUS_RECORDING, SessionManager
from momento.util.format import format_bytes, free_bytes_for
from momento.util.time_format import fmt_time


# Pill colour tokens (background + foreground + dot).
_PILL_RECORDING = ("#3b1f22", "#ff8488", QColor(220, 60, 60))
_PILL_MONITORING = ("#1f2b22", "#7fd99a", QColor(80, 180, 100))
_PILL_IDLE = ("#262a33", "#b8c1d1", QColor(140, 148, 162))


def _dot_pixmap(color: QColor, diameter: int = 10) -> QPixmap:
    # Built lazily inside StatusPanel.__init__ so QApplication exists.
    pm = QPixmap(diameter, diameter)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(1, 1, diameter - 2, diameter - 2)
    finally:
        painter.end()
    return pm


class _Pill(QFrame):
    """Rounded background + dot + label — used for the primary state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusPill")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 12, 4)
        layout.setSpacing(8)
        self._dot = QLabel()
        self._dot.setFixedSize(12, 12)
        layout.addWidget(self._dot)
        self._label = QLabel("")
        layout.addWidget(self._label)

    def set_state(self, dot_pix: QPixmap, label: str, bg: str, fg: str) -> None:
        self._dot.setPixmap(dot_pix)
        self._label.setText(label)
        # Set the style on the pill itself, scoped via objectName so it
        # doesn't bleed onto other QFrames in the editor.
        self.setStyleSheet(
            f"QFrame#StatusPill {{ background: {bg}; border-radius: 12px; }}"
            f"QFrame#StatusPill QLabel {{ color: {fg}; font-weight: 600; "
            f"font-size: 10pt; }}"
        )


class StatusPanel(QFrame):
    """A single-row status strip shown above the editor's main content."""

    def __init__(
        self,
        session: SessionManager,
        config: Config,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._config = config
        self._current_status: str = "idle"
        self._current_game: ActiveGame | None = None

        self._dot_recording = _dot_pixmap(_PILL_RECORDING[2])
        self._dot_monitoring = _dot_pixmap(_PILL_MONITORING[2])
        self._dot_idle = _dot_pixmap(_PILL_IDLE[2])

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "StatusPanel { background-color: #1d2027; border-bottom: 1px solid #262a33; }"
        )
        self.setFixedHeight(46)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 6, 14, 6)
        row.setSpacing(14)

        self._pill = _Pill()
        row.addWidget(self._pill)

        self._duration_label = QLabel("")
        self._duration_label.setStyleSheet("color: #b8c1d1; font-size: 10pt;")
        row.addWidget(self._duration_label)

        row.addStretch(1)

        self._mic_label = QLabel()
        self._mic_label.setStyleSheet("color: #b8c1d1; font-size: 9pt;")
        row.addWidget(self._mic_label)

        self._sys_label = QLabel()
        self._sys_label.setStyleSheet("color: #b8c1d1; font-size: 9pt;")
        row.addWidget(self._sys_label)

        self._disk_label = QLabel("")
        self._disk_label.setStyleSheet("color: #b8c1d1; font-size: 9pt;")
        row.addWidget(self._disk_label)

        self._refresh_audio_labels()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()  # paint initial values

    # ------------------------------------------------------------------ API
    def hideEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Slow the tick when the panel isn't visible — close-to-tray and
        # the settings page both hide the panel for long stretches.
        self._timer.setInterval(5000)
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._timer.setInterval(1000)
        self._tick()  # immediate paint so stale values don't linger
        super().showEvent(event)

    def set_status(self, status: str, game: ActiveGame | None) -> None:
        """Called by the tray when SessionManager pushes a status change."""
        self._current_status = status
        self._current_game = game
        self._tick()

    def set_config(self, config: Config) -> None:
        """Settings were saved — refresh derived labels (mic / system / output)."""
        self._config = config
        self._refresh_audio_labels()
        self._tick()

    # ------------------------------------------------------------- internals
    def _refresh_audio_labels(self) -> None:
        ok_style = "color: #b8c1d1; font-size: 9pt;"
        warn_style = "color: #d4a64a; font-size: 9pt;"
        if self._config.mic_device:
            self._mic_label.setText("Mic: On")
            self._mic_label.setStyleSheet(ok_style)
        else:
            self._mic_label.setText("Mic: Off")
            self._mic_label.setStyleSheet(warn_style)
        if self._config.system_audio_device:
            self._sys_label.setText("System audio: On")
            self._sys_label.setStyleSheet(ok_style)
        else:
            self._sys_label.setText("System audio: Off")
            self._sys_label.setStyleSheet(warn_style)

    def _tick(self) -> None:
        recording = (
            self._current_status == STATUS_RECORDING
            and self._current_game is not None
        )
        if recording:
            display = humanise_game_name(self._current_game.exe_name)
            bg, fg, _ = _PILL_RECORDING
            self._pill.set_state(self._dot_recording, f"Recording {display}", bg, fg)
            elapsed = self._session._recorder.current_position()
            self._duration_label.setText(
                fmt_time(elapsed) if elapsed is not None else ""
            )
        elif self._session.is_monitoring:
            bg, fg, _ = _PILL_MONITORING
            self._pill.set_state(self._dot_monitoring, "Monitoring for games", bg, fg)
            self._duration_label.setText("")
        else:
            bg, fg, _ = _PILL_IDLE
            self._pill.set_state(self._dot_idle, "Idle", bg, fg)
            self._duration_label.setText("")
        self._disk_label.setText(self._disk_text())

    def _disk_text(self) -> str:
        free = free_bytes_for(Path(self._config.output_folder))
        if free is None:
            return "Free space: —"
        return f"Free space: {format_bytes(free)}"
