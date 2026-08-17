"""Continuous live-input level meter.

A horizontal filled bar with a green→yellow→red gradient — looks like the
input meters in OBS / Audacity / Reaper rather than a generic progress
control. Driven externally by :py:meth:`set_level`; decays smoothly to
silence between updates so the bar feels responsive without jittering.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget


_RADIUS = 3
_BG = QColor("#1a1d24")
_FRAME = QColor("#2e333e")
_PEAK_HOLD = QColor("#e6e8ee")
_GREEN = QColor("#3fb058")
_YELLOW = QColor("#d4a64a")
_RED = QColor("#dc3c40")


class LevelMeter(QWidget):
    """Audio peak-level display. Driven from outside by :py:meth:`set_level`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self._peak_hold = 0.0
        self.setMinimumHeight(20)
        self.setMinimumWidth(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._decay_timer = QTimer(self)
        self._decay_timer.setInterval(40)  # ~25 fps decay
        self._decay_timer.timeout.connect(self._decay)

    # ----------------------------------------------------------- public API
    def set_level(self, level: float) -> None:
        """Push a fresh peak value in [0, 1]."""
        level = max(0.0, min(1.0, float(level)))
        self._level = level
        if level > self._peak_hold:
            self._peak_hold = level
        self.update()
        if not self._decay_timer.isActive():
            self._decay_timer.start()

    def reset(self) -> None:
        self._level = 0.0
        self._peak_hold = 0.0
        self._decay_timer.stop()
        self.update()

    # ------------------------------------------------------------- internals
    def _decay(self) -> None:
        # Live bar drops fast; peak-hold lingers so transients stay visible.
        self._level = max(0.0, self._level - 0.06)
        self._peak_hold = max(self._level, self._peak_hold - 0.02)
        self.update()
        if self._level == 0.0 and self._peak_hold == 0.0:
            self._decay_timer.stop()

    # ---------------------------------------------------------------- paint
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            track = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)

            # Track / frame.
            painter.setPen(_FRAME)
            painter.setBrush(_BG)
            painter.drawRoundedRect(track, _RADIUS, _RADIUS)

            inner = track.adjusted(2, 2, -2, -2)
            if inner.width() <= 0 or inner.height() <= 0:
                return

            # Filled bar — gradient spans the *full* width so the colour
            # at any point on the bar matches its position on the meter,
            # not its position relative to the live amplitude.
            gradient = QLinearGradient(inner.left(), 0, inner.right(), 0)
            gradient.setColorAt(0.0, _GREEN)
            gradient.setColorAt(0.70, _GREEN)
            gradient.setColorAt(0.85, _YELLOW)
            gradient.setColorAt(1.0, _RED)

            fill_w = inner.width() * self._level
            if fill_w > 0:
                fill = QRectF(inner.left(), inner.top(), fill_w, inner.height())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(gradient)
                painter.drawRoundedRect(fill, _RADIUS - 1, _RADIUS - 1)

            # Peak-hold tick — thin vertical line at the peak.
            if self._peak_hold > 0 and self._peak_hold >= self._level:
                peak_x = inner.left() + inner.width() * self._peak_hold - 1
                painter.setPen(_PEAK_HOLD)
                painter.drawLine(
                    int(peak_x), int(inner.top()),
                    int(peak_x), int(inner.bottom()),
                )
        finally:
            painter.end()
