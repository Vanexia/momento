"""Branded recording-started/stopped toast notification.

Frameless, always-on-top, click-through-ish, auto-dismissing chip in the
top-right corner of the primary screen — similar to Medal / Discord's
overlay nudges. Single instance owned by the tray; calling show_recording or
show_idle on it replaces whatever was there.
"""

from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QWidget


# Visual tokens (mirror momento.ui.theme so the toast looks like Momento).
_BG = QColor("#1a1d24")
_BORDER = QColor("#34394a")
_ACCENT_REC = QColor("#dc3c40")    # red dot — currently recording
_ACCENT_IDLE = QColor("#8b5cf6")   # violet — recording saved (matches brand accent)
_ACCENT_WARN = QColor("#ecb14c")   # amber — couldn't start, action needed
_ACCENT_BOOKMARK = QColor("#ffaa3c")  # orange — bookmark dropped (matches timeline ticks)
_TITLE = QColor("#f0f1f5")
_SUBTITLE = QColor("#9aa1b1")

# Visible body size. The window itself is larger so the drop shadow has room
# to spill beyond the body without bumping against the window edge — without
# this padding Windows logs UpdateLayeredWindowIndirect failures because the
# shadow's dirty rect extends past the window's destination rect.
_BODY_W = 360
_BODY_H = 84
_SHADOW_PAD = 20   # px on each side
_W = _BODY_W + 2 * _SHADOW_PAD
_H = _BODY_H + 2 * _SHADOW_PAD
_ICON_BOX = 56
_PAD = 12
_RADIUS = 12
_MARGIN_FROM_EDGE = 24   # px from the top-left of the screen

_DEFAULT_DURATION_MS = 4000
_FADE_MS = 220


class RecordingToast(QWidget):
    """A single re-usable toast; call show_recording / show_idle to update."""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # WA_ShowWithoutActivating: don't steal focus from the game.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Mouse events pass through everything outside the painted body — so
        # the user can't accidentally click between the visible card and the
        # window edge.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setFixedSize(_W, _H)

        # Shadow is painted inline in paintEvent (see below) rather than via
        # QGraphicsDropShadowEffect — the effect interacted badly with the
        # frameless + translucent + layered window on Windows and produced
        # `UpdateLayeredWindowIndirect failed` warnings on every show.

        self._title = "Recording started"
        self._subtitle = ""
        # State drives the icon-tint + status-dot colour.
        # "recording" = red, "idle" = blue, "warn" = amber.
        self._state = "recording"
        self._app_icon: QPixmap | None = None
        # Which screen corner the toast lives in. Set via :meth:`set_position`.
        # Same string values as Config.notification_position.
        self._position = "top-left"

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Tracks an in-flight cross-fade "dip" callback so we can disconnect
        # it if another _present interrupts the swap.
        self._on_dip_done = None

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._begin_hide)

    # ---------------------------------------------------------- public API
    def set_app_icon(self, pix: QPixmap | None) -> None:
        self._app_icon = pix
        self.update()

    def show_recording(self, game_name: str | None, duration_ms: int = _DEFAULT_DURATION_MS) -> None:
        self._title = "Recording started"
        self._subtitle = (
            f"{game_name} is being recorded" if game_name else "Your gameplay is being recorded"
        )
        self._state = "recording"
        self._present(duration_ms)

    def show_idle(self, game_name: str | None, duration_ms: int = _DEFAULT_DURATION_MS) -> None:
        self._title = "Recording saved"
        self._subtitle = (
            f"{game_name} clip saved to your recordings folder"
            if game_name
            else "Clip saved to your recordings folder"
        )
        self._state = "idle"
        self._present(duration_ms)

    def show_warning(
        self, title: str, subtitle: str, duration_ms: int = _DEFAULT_DURATION_MS * 2
    ) -> None:
        """Amber variant — surfaces "couldn't record" problems the user must fix.

        Lingers twice as long as a normal toast because the user has to act on it.
        """
        self._title = title
        self._subtitle = subtitle
        self._state = "warn"
        self._present(duration_ms)

    def show_bookmark(
        self,
        game_name: str | None,
        elapsed_seconds: float,
        duration_ms: int = _DEFAULT_DURATION_MS // 2,
    ) -> None:
        """Orange variant — confirms a bookmark hotkey press during recording.

        Shows the timestamp the bookmark landed at, so the user knows the
        capture actually happened (the hotkey is otherwise silent). Lingers
        for half the default — it's a passive ack, not something they have
        to read; we don't want it sitting over the gameplay.
        """
        self._title = "Bookmark added"
        time_str = _fmt_short_time(elapsed_seconds)
        self._subtitle = (
            f"{game_name} @ {time_str}" if game_name else f"Marked at {time_str}"
        )
        self._state = "bookmark"
        self._present(duration_ms)

    # ---------------------------------------------------------- internals
    def _present(self, duration_ms: int) -> None:
        self._reposition()
        self._dismiss_timer.stop()
        self._fade.stop()
        self._disconnect_hide()
        self._disconnect_dip()

        # If a toast is already on screen, cross-fade between the old and
        # new contents instead of snapping. Without this, hitting bookmark
        # while "Recording started" is still up produced a hard red->orange
        # color flash with the text snapping mid-animation.
        if self.isVisible() and self.windowOpacity() > 0.1:
            self._begin_cross_fade(duration_ms)
            return

        # Cold show — paint with new content, then fade in from invisible.
        self.update()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._start_rise(duration_ms)

    def _begin_cross_fade(self, duration_ms: int) -> None:
        """Dip current opacity to 0 (quick), swap repaint, then rise."""
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.setDuration(120)

        def on_dip_done() -> None:
            self._disconnect_dip()
            self.update()  # repaint with the new title/state/colour
            self._start_rise(duration_ms)

        self._on_dip_done = on_dip_done
        self._fade.finished.connect(on_dip_done)
        self._fade.start()

    def _start_rise(self, duration_ms: int) -> None:
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setDuration(_FADE_MS)
        self._fade.start()
        if duration_ms > 0:
            self._dismiss_timer.start(duration_ms)

    def _disconnect_dip(self) -> None:
        if self._on_dip_done is not None:
            try:
                self._fade.finished.disconnect(self._on_dip_done)
            except TypeError:
                pass
            self._on_dip_done = None

    def _begin_hide(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        # Make sure we only have one hide-on-finished connection at a time.
        self._disconnect_hide()
        self._fade.finished.connect(self.hide)
        self._fade.start()

    def _disconnect_hide(self) -> None:
        try:
            self._fade.finished.disconnect(self.hide)
        except TypeError:
            pass  # wasn't connected — fine

    def set_position(self, position: str) -> None:
        """Pin the toast to a corner of the primary screen. ``position`` is
        one of ``top-left`` / ``top-right`` / ``bottom-left`` /
        ``bottom-right`` (Config.notification_position values)."""
        self._position = position
        if self.isVisible():
            self._reposition()

    def _reposition(self) -> None:
        from PyQt6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        # Default to top-left to match historical behaviour if the position
        # string is unrecognised. Margins use the widget's full size
        # (including shadow padding) so the visible card sits a uniform
        # distance from the screen edge regardless of corner.
        position = self._position if self._position else "top-left"
        left = geo.left() + _MARGIN_FROM_EDGE
        right = geo.left() + geo.width() - _W - _MARGIN_FROM_EDGE
        top = geo.top() + _MARGIN_FROM_EDGE
        bottom = geo.top() + geo.height() - _H - _MARGIN_FROM_EDGE
        if position == "top-right":
            x, y = right, top
        elif position == "bottom-left":
            x, y = left, bottom
        elif position == "bottom-right":
            x, y = right, bottom
        else:  # top-left
            x, y = left, top
        self.move(QPoint(x, y))

    # ---------------------------------------------------------- paint
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Body rect, inset by the shadow padding.
        body_rect = QRect(_SHADOW_PAD, _SHADOW_PAD, _BODY_W, _BODY_H)

        # Hand-rolled drop shadow: a few translucent rounded rects offset
        # progressively, drawn before the body. Cheap and avoids the
        # QGraphicsEffect / layered-window incompatibility.
        for i, alpha in enumerate((40, 30, 18, 10, 6)):
            spread = (i + 1) * 3
            shadow = body_rect.adjusted(-spread, -spread + 2, spread, spread + 2)
            sp = QPainterPath()
            sp.addRoundedRect(shadow.toRectF(), _RADIUS + spread, _RADIUS + spread)
            painter.fillPath(sp, QColor(0, 0, 0, alpha))

        # Pill-rounded body background with subtle border.
        path = QPainterPath()
        path.addRoundedRect(body_rect.toRectF(), _RADIUS, _RADIUS)
        painter.fillPath(path, _BG)
        painter.setPen(QPen(_BORDER, 1))
        painter.drawPath(path)

        # Left icon area (square with rounded inner box for the app icon).
        icon_rect = QRect(
            body_rect.left() + _PAD,
            body_rect.top() + (_BODY_H - _ICON_BOX) // 2,
            _ICON_BOX,
            _ICON_BOX,
        )
        # Soft tinted background that switches colour based on state.
        tint = {
            "recording": _ACCENT_REC,
            "idle": _ACCENT_IDLE,
            "warn": _ACCENT_WARN,
            "bookmark": _ACCENT_BOOKMARK,
        }.get(self._state, _ACCENT_REC)
        bg_tint = QColor(tint)
        bg_tint.setAlpha(40)
        icon_bg = QPainterPath()
        icon_bg.addRoundedRect(icon_rect.toRectF(), 10, 10)
        painter.fillPath(icon_bg, bg_tint)

        # The app icon (resolved by the tray and handed in via set_app_icon).
        if self._app_icon is not None and not self._app_icon.isNull():
            # Tight inset — the .ico already has its own rounded-square frame
            # with margin, so we don't need much more here.
            inset = 2
            target = icon_rect.adjusted(inset, inset, -inset, -inset)
            scaled = self._app_icon.scaled(
                target.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            tx = target.left() + (target.width() - scaled.width()) // 2
            ty = target.top() + (target.height() - scaled.height()) // 2
            painter.drawPixmap(tx, ty, scaled)

        # Status indicator dot — bottom-right of the icon box.
        dot_r = 9
        cx = icon_rect.right() - dot_r + 2
        cy = icon_rect.bottom() - dot_r + 2
        painter.setPen(QPen(_BG, 2))  # halo to lift it off the icon
        painter.setBrush(tint)
        painter.drawEllipse(cx - dot_r, cy - dot_r, dot_r * 2, dot_r * 2)

        # Text column to the right of the icon.
        text_left = icon_rect.right() + 14
        text_right = body_rect.right() - _PAD
        text_w = text_right - text_left

        title_font = QFont(self.font())
        title_font.setPointSize(12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(_TITLE)
        title_metrics = QFontMetrics(title_font)
        title_y = body_rect.top() + (_BODY_H // 2) - 2
        elided_title = title_metrics.elidedText(
            self._title, Qt.TextElideMode.ElideRight, text_w
        )
        painter.drawText(text_left, title_y, elided_title)

        subtitle_font = QFont(self.font())
        subtitle_font.setPointSize(9)
        painter.setFont(subtitle_font)
        painter.setPen(_SUBTITLE)
        sub_metrics = QFontMetrics(subtitle_font)
        sub_y = title_y + sub_metrics.height() + 4
        elided_sub = sub_metrics.elidedText(
            self._subtitle, Qt.TextElideMode.ElideRight, text_w
        )
        painter.drawText(text_left, sub_y, elided_sub)

    # Don't ever take focus from the game.
    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt API)
        event.ignore()

    # Clicking the toast just dismisses it.
    def mousePressEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        self._begin_hide()


def _fmt_short_time(seconds: float) -> str:
    """`12.4s -> 0:12`, `83s -> 1:23`, `3725s -> 1:02:05`. Compact form for toasts."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"
