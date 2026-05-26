"""Shared custom widgets for Momento's UI."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPaintEvent, QPainter, QPen
from PyQt6.QtWidgets import QComboBox

from momento.ui.theme import TEXT_DIM, TEXT_DIM_2


def _build_chevron_pen(colour: str) -> QPen:
    pen = QPen(QColor(colour))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


class AnchoredComboBox(QComboBox):
    """Drop-in QComboBox replacement with:

    * Popup anchored directly below the field (default Windows behaviour
      shifts the popup so the selected row sits under the cursor).
    * A chevron painted at the right edge — Fusion drops the platform
      arrow once the global QSS themes ``::drop-down``, so we paint our
      own.
    """

    _CHEVRON_WIDTH = 8
    _CHEVRON_HEIGHT = 4
    _CHEVRON_RIGHT_PAD = 9

    # Cached pens — avoids reallocating per repaint across every dropdown
    # in the program.
    _PEN_ENABLED = _build_chevron_pen(TEXT_DIM)
    _PEN_DISABLED = _build_chevron_pen(TEXT_DIM_2)

    def showPopup(self) -> None:  # noqa: N802 (Qt API)
        super().showPopup()
        popup = self.view().window()
        if popup is None:
            return
        popup.move(self.mapToGlobal(self.rect().bottomLeft()))

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt API)
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self._PEN_ENABLED if self.isEnabled() else self._PEN_DISABLED)
        right = self.width() - self._CHEVRON_RIGHT_PAD
        cy = self.height() / 2
        left = right - self._CHEVRON_WIDTH
        mid_x = right - self._CHEVRON_WIDTH / 2
        top_y = cy - self._CHEVRON_HEIGHT / 2
        bot_y = cy + self._CHEVRON_HEIGHT / 2
        painter.drawLine(QPointF(left, top_y), QPointF(mid_x, bot_y))
        painter.drawLine(QPointF(mid_x, bot_y), QPointF(right, top_y))
