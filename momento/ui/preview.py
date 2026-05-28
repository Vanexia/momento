"""Video preview widget — QMediaPlayer + QVideoWidget.

Public API consumed by the editor + timeline:

    load(path)              — load an MP4/MKV (auto-pauses)
    play() / pause() / toggle_play()
    seek(seconds)
    play_range(start, end)  — play just the trim range, auto-pause at end
    position() -> seconds
    duration() -> seconds

Signals: position_changed, duration_changed, playback_state_changed,
error_occurred.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QSize, QTimer,
    QUrl, pyqtProperty, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QCursor, QIcon, QKeyEvent, QKeySequence, QMouseEvent, QPainter,
    QPalette, QPen, QPixmap, QPolygonF, QShortcut,
)
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt

from momento.ui import theme as _theme
from momento.util.time_format import fmt_time

logger = logging.getLogger(__name__)


class _VideoArea(QVideoWidget):
    """QVideoWidget that publishes mouse / keyboard events the preview cares
    about (click to play/pause, double-click for fullscreen, Escape to exit
    fullscreen).

    Subclassing is the cheapest way to catch these — Qt's event filtering on
    QVideoWidget is finicky because video rendering uses a native sub-window.
    """

    clicked = pyqtSignal()
    double_clicked = pyqtSignal()
    escape_pressed = pyqtSignal()
    # Fires for any cursor movement over the widget. Used by the fullscreen
    # overlay to reveal itself + reset its auto-hide timer.
    mouse_moved = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # StrongFocus so keyPressEvent fires after a click and in fullscreen.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        # Why: QVideoWidget's default sizeHint() tracks the current video's
        # native resolution, which makes the splitter re-apportion the
        # preview every clip-load. Returning a fixed hint decouples layout
        # from video resolution.
        return QSize(320, 180)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        # Why fire on press: Qt's double-click sequence is press → release →
        # doubleClick → release (the second press is replaced), so we toggle
        # once here and mouseDoubleClickEvent toggles again to cancel — net
        # "click = play, double-click = fullscreen with play state preserved",
        # without a double-click-interval wait. Same model YouTube/VLC use.
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            # Undo the play-toggle from the preceding press so a double-click
            # only changes fullscreen state, not play state.
            self.clicked.emit()
        self.double_clicked.emit()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt API)
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _ClickJumpSlider(QSlider):
    """QSlider that jumps the handle to wherever you click.

    Stock QSlider treats a groove-click as "step by pageStep" — pressing
    the track on a volume slider at 10 % only moves to 20 %, so reaching
    100 % needs nine clicks. This makes any click compute the value from
    pixel position and snaps the handle there. Dragging continues to
    work as normal.
    """

    def _value_for_x(self, x: float) -> int:
        ratio = max(0.0, min(1.0, x / max(1, self.width())))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._value_for_x(event.position().x()))
            self.sliderPressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.setValue(self._value_for_x(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)


class _ProgressBar(QWidget):
    """Thin click-to-seek progress bar.

    The visible bar is 5 px, but the click hit area is 18 px tall so the
    user doesn't have to pixel-hunt. Hovering thickens the bar to 7 px —
    matches the YouTube/Medal "hover to expand" affordance.
    """

    seek_requested = pyqtSignal(float)         # 0..1 ratio
    scrubbing_changed = pyqtSignal(bool)

    _BAR_HEIGHT = 5
    _BAR_HOVER_HEIGHT = 7
    _HIT_HEIGHT = 18
    _HANDLE_RADIUS = 6
    _HANDLE_HOVER_RADIUS = 7
    _COLOUR_TRACK = QColor(255, 255, 255, 80)
    _COLOUR_HANDLE = QColor("white")

    @property
    def _COLOUR_FILL(self) -> QColor:  # noqa: N802 (matches existing class-constant style)
        # Read from theme at paint time so accent shifts carry through.
        return QColor(_theme.ACCENT)

    # Hover/scrub thickening is animated via this property — bar height
    # and handle radius interpolate together so both swell in lockstep
    # instead of snapping.
    _SWELL_DURATION_MS = 150

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(self._HIT_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self._progress = 0.0
        self._scrubbing = False
        self._swell = 0.0
        self._swell_anim = QPropertyAnimation(self, b"swell", self)
        self._swell_anim.setDuration(self._SWELL_DURATION_MS)
        self._swell_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def is_scrubbing(self) -> bool:
        return self._scrubbing

    def set_progress(self, ratio: float) -> None:
        if self._scrubbing:
            return
        new = max(0.0, min(1.0, ratio))
        if abs(new - self._progress) < 0.0005:
            return
        self._progress = new
        self.update()

    # pyqtProperty so QPropertyAnimation can drive it. Setting it
    # repaints; getter is the source of truth for paintEvent.
    def _get_swell(self) -> float:
        return self._swell

    def _set_swell(self, value: float) -> None:
        if abs(value - self._swell) < 0.001:
            return
        self._swell = value
        self.update()

    swell = pyqtProperty(float, _get_swell, _set_swell)

    def _animate_swell_to(self, target: float) -> None:
        self._swell_anim.stop()
        self._swell_anim.setStartValue(self._swell)
        self._swell_anim.setEndValue(target)
        self._swell_anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        # Interpolate geometry by self._swell (0..1) so hover/scrub
        # transitions visibly swell instead of snapping.
        bar_h = self._BAR_HEIGHT + (self._BAR_HOVER_HEIGHT - self._BAR_HEIGHT) * self._swell
        y = (self.height() - bar_h) / 2
        painter.fillRect(QRectF(0, y, self.width(), bar_h), self._COLOUR_TRACK)
        if self._progress > 0:
            w = self.width() * self._progress
            painter.fillRect(QRectF(0, y, w, bar_h), self._COLOUR_FILL)
        # Playhead handle — anti-aliased; clamp centre so it never
        # bleeds past the bar's edges.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = self._HANDLE_RADIUS + (self._HANDLE_HOVER_RADIUS - self._HANDLE_RADIUS) * self._swell
        cx = max(radius, min(self.width() - radius, self.width() * self._progress))
        cy = self.height() / 2
        painter.setBrush(self._COLOUR_HANDLE)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

    def enterEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        self._animate_swell_to(1.0)

    def leaveEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        if not self._scrubbing:
            self._animate_swell_to(0.0)

    def _ratio_for_x(self, x: float) -> float:
        return max(0.0, min(1.0, x / max(1, self.width())))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self._scrubbing = True
            self.scrubbing_changed.emit(True)
            self._animate_swell_to(1.0)
            self._progress = self._ratio_for_x(event.position().x())
            self.update()
            self.seek_requested.emit(self._progress)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if self._scrubbing:
            self._progress = self._ratio_for_x(event.position().x())
            self.update()
            self.seek_requested.emit(self._progress)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton and self._scrubbing:
            self._scrubbing = False
            self.scrubbing_changed.emit(False)
            if not self.underMouse():
                self._animate_swell_to(0.0)
            event.accept()
            return
        super().mouseReleaseEvent(event)


# Custom-painted monochrome white icons — Qt's standardIcon() ships
# coloured Fusion glyphs that vanish on a dark gradient.
def _glyph_icon(paint_fn, size: int = 22) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    paint_fn(p, float(size))
    p.end()
    return QIcon(pix)


def _paint_play(p: QPainter, s: float) -> None:
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.32, s * 0.20),
        QPointF(s * 0.80, s * 0.50),
        QPointF(s * 0.32, s * 0.80),
    ]))


def _paint_pause(p: QPainter, s: float) -> None:
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    bar_w = s * 0.18
    h = s * 0.60
    y = s * 0.20
    p.drawRect(QRectF(s * 0.30, y, bar_w, h))
    p.drawRect(QRectF(s * 0.52, y, bar_w, h))


def _paint_volume(p: QPainter, s: float) -> None:
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.10, s * 0.38),
        QPointF(s * 0.28, s * 0.38),
        QPointF(s * 0.46, s * 0.18),
        QPointF(s * 0.46, s * 0.82),
        QPointF(s * 0.28, s * 0.62),
        QPointF(s * 0.10, s * 0.62),
    ]))
    p.setBrush(Qt.BrushStyle.NoBrush)
    pen = QPen(QColor("white"), s * 0.07)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawArc(QRectF(s * 0.50, s * 0.32, s * 0.18, s * 0.36), -60 * 16, 120 * 16)
    p.drawArc(QRectF(s * 0.56, s * 0.22, s * 0.30, s * 0.56), -60 * 16, 120 * 16)


def _paint_mute(p: QPainter, s: float) -> None:
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.10, s * 0.38),
        QPointF(s * 0.28, s * 0.38),
        QPointF(s * 0.46, s * 0.18),
        QPointF(s * 0.46, s * 0.82),
        QPointF(s * 0.28, s * 0.62),
        QPointF(s * 0.10, s * 0.62),
    ]))
    pen = QPen(QColor("white"), s * 0.09)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(s * 0.58, s * 0.38), QPointF(s * 0.86, s * 0.62))
    p.drawLine(QPointF(s * 0.86, s * 0.38), QPointF(s * 0.58, s * 0.62))


def _paint_exit_fullscreen(p: QPainter, s: float) -> None:
    pen = QPen(QColor("white"), s * 0.10)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    m = s * 0.22
    arm = s * 0.20
    # Four L-brackets pointing inward.
    p.drawPolyline(QPolygonF([
        QPointF(m, m + arm), QPointF(m, m), QPointF(m + arm, m),
    ]))
    p.drawPolyline(QPolygonF([
        QPointF(s - m - arm, m), QPointF(s - m, m), QPointF(s - m, m + arm),
    ]))
    p.drawPolyline(QPolygonF([
        QPointF(m, s - m - arm), QPointF(m, s - m), QPointF(m + arm, s - m),
    ]))
    p.drawPolyline(QPolygonF([
        QPointF(s - m, s - m - arm), QPointF(s - m, s - m), QPointF(s - m - arm, s - m),
    ]))


class _FullscreenOverlay(QWidget):
    """Medal-style floating bar: thin progress line + minimal icon row.

    Top-level frameless window positioned over the host's bottom edge.
    Auto-hides after ``HIDE_DELAY_MS`` of cursor inactivity; reappears on
    any mouse movement. Fade is a ``QGraphicsOpacityEffect`` on the
    ``_content`` child widget — applying the effect to the top-level
    fights ``WA_TranslucentBackground``.
    """

    HIDE_DELAY_MS = 2500
    FADE_DURATION_MS = 220
    MOUSE_POLL_MS = 60

    def __init__(self, host: "_VideoArea", preview: "VideoPreview") -> None:
        super().__init__(None)
        self._preview = preview
        self._host = host
        self._last_cursor_pos: QPointF | None = None

        # Tool window: floats above host, doesn't appear in taskbar.
        # No WindowDoesNotAcceptFocus — that flag eats button clicks on
        # Windows. Buttons use NoFocus so they receive clicks without
        # stealing keyboard focus from the host.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("overlay")
        self.setStyleSheet(_OVERLAY_QSS)
        self.setMouseTracking(True)

        # Pre-build the icon set as monochrome white pixmaps so they
        # render against the dark gradient (Qt's standardIcon glyphs are
        # platform-coloured and disappear).
        self._icon_play = _glyph_icon(_paint_play)
        self._icon_pause = _glyph_icon(_paint_pause)
        self._icon_volume = _glyph_icon(_paint_volume)
        self._icon_mute = _glyph_icon(_paint_mute)
        self._icon_exit = _glyph_icon(_paint_exit_fullscreen)

        # --- content wrapper -------------------------------------------
        # Everything goes inside this child widget so the fade animation
        # can run a QGraphicsOpacityEffect on the child without fighting
        # the window's WA_TranslucentBackground. Mouse hits on the child
        # still register because opacity effects don't block events.
        self._content = QWidget(self)
        self._content.setObjectName("content")
        self._content.setMouseTracking(True)

        # --- progress bar (top) ---------------------------------------
        self._progress = _ProgressBar()
        self._progress.seek_requested.connect(self._on_seek_ratio)
        self._progress.scrubbing_changed.connect(self._on_scrubbing_changed)

        # --- control row ----------------------------------------------
        self._play_btn = self._make_icon_button(self._icon_play, "Play / pause")
        self._play_btn.clicked.connect(self._on_play_clicked)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setObjectName("time")

        self._mute_btn = self._make_icon_button(self._icon_volume, "Mute")
        self._mute_btn.clicked.connect(self._on_mute_clicked)

        self._volume_slider = _ClickJumpSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(int(round(preview._audio.volume() * 100)))
        # Width leaves room for the 12 px handle at both ends; explicit
        # height gives the handle vertical clearance too — its QSS
        # ``margin: -4px 0`` extends past the groove so the slider needs
        # to be at least 16 px tall to avoid clipping the top.
        self._volume_slider.setFixedWidth(104)
        self._volume_slider.setFixedHeight(22)
        self._volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._volume_slider.setObjectName("volume")
        self._volume_slider.valueChanged.connect(preview._on_volume_changed)
        # Keep the embedded preview's slider in sync.
        self._volume_slider.valueChanged.connect(preview._volume_slider.setValue)

        self._exit_btn = self._make_icon_button(
            self._icon_exit, "Exit fullscreen (F or Esc)"
        )
        self._exit_btn.clicked.connect(self._on_exit_clicked)

        # --- layout ---------------------------------------------------
        controls = QHBoxLayout()
        controls.setContentsMargins(20, 6, 20, 12)
        controls.setSpacing(8)
        controls.addWidget(self._play_btn)
        controls.addSpacing(4)
        controls.addWidget(self._time_label)
        controls.addStretch(1)
        controls.addWidget(self._mute_btn)
        controls.addWidget(self._volume_slider)
        controls.addSpacing(12)
        controls.addWidget(self._exit_btn)

        controls_box = QWidget()
        controls_box.setObjectName("controls")
        controls_box.setLayout(controls)

        content_lay = QVBoxLayout(self._content)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(0)
        content_lay.addWidget(self._progress)
        content_lay.addWidget(controls_box)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._content)

        # --- fade (opacity effect on the child, not the window) -------
        # Initial opacity 0 so the on_activity() call at the end of
        # __init__ animates the bar into view — entering fullscreen
        # without a fade-in feels like the bar "pops" on screen.
        self._opacity = QGraphicsOpacityEffect(self._content)
        self._opacity.setOpacity(0.0)
        self._content.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(self.FADE_DURATION_MS)
        # Ease-out: instant visual lead-in (where the user's attention is
        # highest), gentle settle. Linear opacity ramps feel mechanical.
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self.HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._fade_out)

        # Cursor-position polling: QVideoWidget's native sub-window can
        # swallow mouseMoveEvent so the host signal isn't reliable. Polling
        # global cursor position catches every move regardless.
        self._cursor_poll = QTimer(self)
        self._cursor_poll.setInterval(self.MOUSE_POLL_MS)
        self._cursor_poll.timeout.connect(self._poll_cursor)
        self._cursor_poll.start()

        # --- wire to player ------------------------------------------
        preview.position_changed.connect(self._on_position_changed)
        preview.duration_changed.connect(self._on_duration_changed)
        preview.playback_state_changed.connect(self._on_playing_changed)
        host.mouse_moved.connect(self.on_activity)
        host.installEventFilter(self)

        # Bounce focus back to host on any overlay activation — clicks
        # on controls otherwise steal the host's keyboard shortcuts.
        app = QApplication.instance()
        if app is not None:
            app.focusWindowChanged.connect(self._on_focus_window_changed)

        # Mirror the host shortcuts on the overlay for the brief window
        # where focus might still be on us before the bounce lands.
        def _shortcut(seq: str, slot) -> None:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

        _shortcut("Escape", preview.toggle_fullscreen)
        _shortcut("F", preview.toggle_fullscreen)
        _shortcut("Space", preview.toggle_play)
        _shortcut("M", preview.toggle_mute)

        self._anchor_to_host()
        self.on_activity()

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _make_icon_button(icon: QIcon, tooltip: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(38, 38)
        btn.setIcon(icon)
        btn.setIconSize(QSize(22, 22))
        btn.setToolTip(tooltip)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    # ----------------------------------------------------------- public
    def on_activity(self) -> None:
        """Reveal the overlay and (re)start the auto-hide timer."""
        if self._fade.state() != QPropertyAnimation.State.Stopped or self._opacity.opacity() < 1.0:
            self._fade.stop()
            self._fade.setStartValue(self._opacity.opacity())
            self._fade.setEndValue(1.0)
            self._fade.start()
        self._host.unsetCursor()
        # Always restart the hide timer on activity — including when the
        # cursor is hovering controls. That matches YouTube/Medal: stop
        # moving and the bar fades regardless of cursor position.
        if self._progress.is_scrubbing():
            self._hide_timer.stop()
        else:
            self._hide_timer.start()

    # ----------------------------------------------------------- private
    def eventFilter(self, watched, event):  # noqa: N802 (Qt API)
        if watched is self._host and event.type() in (
            QEvent.Type.Resize, QEvent.Type.Move,
        ):
            self._anchor_to_host()
        return False

    def _poll_cursor(self) -> None:
        pos = QCursor.pos()
        if self._last_cursor_pos is not None and pos == self._last_cursor_pos:
            return
        self._last_cursor_pos = pos
        # Cursor moves on another monitor mustn't wake the overlay — the
        # user is doing something else there. Only count motion that lands
        # inside the fullscreen host's screen.
        if not self._host.geometry().contains(pos):
            return
        self.on_activity()

    def _on_focus_window_changed(self, window) -> None:
        """If focus accidentally landed on the overlay (the user clicked
        a control), bounce it straight back to the host so keyboard
        shortcuts keep firing there."""
        if window is None:
            return
        try:
            our_window = self.windowHandle()
        except RuntimeError:
            # Overlay being torn down — listener should self-disconnect.
            return
        if window is our_window:
            QTimer.singleShot(0, self._return_focus_to_host)

    def _anchor_to_host(self) -> None:
        host_geom = self._host.geometry()
        top_left = self._host.mapToGlobal(self._host.rect().topLeft())
        h = self.sizeHint().height()
        self.setGeometry(
            top_left.x(),
            top_left.y() + host_geom.height() - h,
            host_geom.width(),
            h,
        )

    def _fade_out(self) -> None:
        if self._progress.is_scrubbing():
            return
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        self._host.setCursor(Qt.CursorShape.BlankCursor)

    def _return_focus_to_host(self) -> None:
        self._host.activateWindow()
        self._host.setFocus(Qt.FocusReason.OtherFocusReason)

    # --- slots: control row ---
    def _on_play_clicked(self) -> None:
        self._preview.toggle_play()
        self._return_focus_to_host()

    def _on_mute_clicked(self) -> None:
        self._preview.toggle_mute()
        muted = self._preview._mute_btn.isChecked()
        self._mute_btn.setIcon(self._icon_mute if muted else self._icon_volume)
        self._return_focus_to_host()

    def _on_exit_clicked(self) -> None:
        self._preview.toggle_fullscreen()

    # --- slots: scrubbing ---
    def _on_seek_ratio(self, ratio: float) -> None:
        dur = self._preview.duration()
        if dur > 0:
            self._preview.seek(ratio * dur)
        self._fade.stop()
        self._opacity.setOpacity(1.0)
        self._host.unsetCursor()
        self._hide_timer.stop()

    def _on_scrubbing_changed(self, scrubbing: bool) -> None:
        if not scrubbing:
            self.on_activity()

    # --- slots: player events ---
    def _on_position_changed(self, seconds: float) -> None:
        dur = self._preview.duration()
        if dur > 0:
            self._progress.set_progress(seconds / dur)
        self._update_time_label()

    def _on_duration_changed(self, _seconds: float) -> None:
        self._update_time_label()

    def _on_playing_changed(self, playing: bool) -> None:
        self._play_btn.setIcon(self._icon_pause if playing else self._icon_play)

    def _update_time_label(self) -> None:
        cur = self._preview.position()
        dur = self._preview.duration()
        self._time_label.setText(f"{fmt_time(cur)} / {fmt_time(dur)}")


_OVERLAY_QSS = """
QWidget#overlay { background: transparent; }
QWidget#overlay QWidget#content { background: transparent; }
QWidget#overlay QWidget#controls {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(0,0,0,0), stop:0.5 rgba(0,0,0,170), stop:1 rgba(0,0,0,215));
}
QWidget#overlay QLabel#time {
    color: white;
    font-size: 11pt;
    font-variant-numeric: tabular-nums;
    padding: 0 6px;
}
QWidget#overlay QPushButton {
    background: transparent;
    border: none;
    color: white;
    padding: 0;
    border-radius: 6px;
}
QWidget#overlay QPushButton:hover {
    background: rgba(255, 255, 255, 38);
}
QWidget#overlay QPushButton:pressed {
    background: rgba(255, 255, 255, 64);
}
QWidget#overlay QSlider#volume {
    /* Horizontal padding gives the handle room to sit at both ends
       without bleeding past the slider widget's edges. */
    padding: 0 6px;
}
QWidget#overlay QSlider#volume::groove:horizontal {
    height: 4px;
    background: rgba(255, 255, 255, 60);
    border-radius: 2px;
}
QWidget#overlay QSlider#volume::sub-page:horizontal {
    background: white;
    border-radius: 2px;
}
QWidget#overlay QSlider#volume::add-page:horizontal {
    background: rgba(255, 255, 255, 60);
    border-radius: 2px;
}
QWidget#overlay QSlider#volume::handle:horizontal {
    background: white;
    width: 12px; height: 12px;
    margin: -4px 0;
    border-radius: 6px;
    /* Override the global ``QSlider::handle:horizontal`` rule that paints
       a 2 px ring around every handle — that ring was the source of the
       top clip (made the effective handle 16 px tall, not 12). */
    border: none;
}
"""


class VideoPreview(QWidget):
    """A self-contained video preview with play/pause controls.

    Seeking is owned by the Timeline widget below — it has click-to-seek,
    trim handles, bookmark ticks, and a playhead. The preview row here
    only carries the play button, time readout, and volume controls.
    """

    position_changed = pyqtSignal(float)  # seconds
    duration_changed = pyqtSignal(float)  # seconds
    playback_state_changed = pyqtSignal(bool)  # True when playing
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_path: Path | None = None
        self._volume_before_mute = 0.8
        # When set, the position watcher auto-pauses when the playhead
        # crosses this value. Used by :meth:`play_range` so the user can
        # preview just the trimmed portion. Cleared by any subsequent
        # explicit seek so the user can step back into normal playback.
        self._pause_at_seconds: float | None = None

        # Seek-time mute book-keeping. H.264 seeks force the decoder to jump
        # to the nearest keyframe and forward-decode to the requested frame;
        # whatever audio buffer plays during that catch-up sounds like
        # mangled noise + a "video speed-up" once it settles. Muting briefly
        # across every seek covers it.
        self._mute_by_seek = False
        self._seek_mute_timer = QTimer(self)
        self._seek_mute_timer.setSingleShot(True)
        self._seek_mute_timer.timeout.connect(self._end_seek_mute)

        # Fullscreen host. Lazily built when entering fullscreen, destroyed
        # on exit. Implemented as a SEPARATE top-level QVideoWidget rather
        # than reparenting the embedded one — reparenting is racy with Qt's
        # window-flag plumbing on Windows (manifested as the widget showing
        # at editor-sized then snapping to fullscreen).
        self._fullscreen_host: _VideoArea | None = None
        self._fullscreen_overlay: _FullscreenOverlay | None = None

        # First-frame priming: after setSource, the QVideoWidget shows
        # black until something decodes a frame for it. Briefly play and
        # immediately pause once durationChanged arrives so the preview
        # opens on the actual first frame instead of a black void. The
        # existing play→pause mute-blink swallows the audio buffer prime.
        self._prime_pending = False

        # External duration hint from ffprobe (see media_probe.probe_duration_async).
        # Used when QMediaPlayer's WMF backend reports duration=0 for an MKV
        # whose segment header was never finalised — without this the
        # timeline is stuck at 0 and trim is unreachable.
        self._duration_hint_ms = 0

        # --- player ---
        # NOTE: in Qt 6 the player needs an explicit QAudioOutput attached
        # BEFORE setSource() is called, otherwise the audio track is decoded
        # but silently dropped. Both outputs are set right after the player
        # is constructed, so load() never has to worry about ordering.
        self._player = QMediaPlayer(self)
        # Route preview audio to the current Windows default output. We hold a
        # QMediaDevices instance so its audioOutputsChanged signal fires when
        # the user picks a different default in Windows — we then point the
        # QAudioOutput at the new default. No UI knob required.
        self._media_devices = QMediaDevices(self)
        self._audio = QAudioOutput(QMediaDevices.defaultAudioOutput(), self)
        self._audio.setVolume(self._volume_before_mute)
        self._audio.setMuted(False)
        self._player.setAudioOutput(self._audio)
        self._media_devices.audioOutputsChanged.connect(self._sync_audio_to_default)
        logger.info("Preview audio bound to default: %r", self._audio.device().description())
        self._video_widget = _VideoArea(self)
        self._video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._video_widget.setMinimumHeight(200)
        # The video renders into a native OS sub-window that the dark theme's
        # QSS/palette doesn't reach, so on Windows it erases to white for a
        # frame on every window show — a visible flash, since the preview
        # fills a big slice of the editor. Force its background brush dark so
        # any pre-frame erase blends into the theme (video letterboxes on
        # black anyway) instead of flashing white.
        self._video_widget.setAutoFillBackground(True)
        _vpal = self._video_widget.palette()
        _vpal.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self._video_widget.setPalette(_vpal)
        self._video_widget.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._video_widget.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._video_widget.clicked.connect(self.toggle_play)
        self._video_widget.double_clicked.connect(self.toggle_fullscreen)
        self._video_widget.escape_pressed.connect(self._exit_fullscreen_if_active)
        self._player.setVideoOutput(self._video_widget)

        # Stack the video widget alongside an empty-state placeholder so the
        # preview never opens on raw black when nothing is loaded.
        self._video_empty_label = QLabel(
            "Select a recording to preview it here."
        )
        self._video_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_empty_label.setWordWrap(True)
        self._video_empty_label.setStyleSheet(
            "QLabel { background-color: #0e1117; color: #8a92a3; font-size: 11pt; }"
        )
        self._video_stack = QStackedWidget()
        self._video_stack.addWidget(self._video_widget)
        self._video_stack.addWidget(self._video_empty_label)
        self._video_stack.setCurrentIndex(1)  # start on the placeholder

        # --- controls ---
        self._play_btn = QPushButton()
        self._play_btn.setFixedWidth(36)
        self._set_play_icon(playing=False)
        self._play_btn.clicked.connect(self.toggle_play)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setMinimumWidth(96)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._mute_btn = QPushButton()
        self._mute_btn.setFixedWidth(32)
        self._mute_btn.setCheckable(True)
        self._mute_btn.toggled.connect(self._on_mute_toggled)
        self._set_mute_icon(muted=False)

        self._volume_slider = _ClickJumpSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(int(round(self._volume_before_mute * 100)))
        self._volume_slider.setFixedWidth(110)
        # Global theme paints a 14 px handle with a 2 px border (18 px
        # total) plus negative top margin — without an explicit height
        # the slider sizes itself too tight and clips the handle's top.
        self._volume_slider.setFixedHeight(24)
        self._volume_slider.setToolTip("Preview volume (this affects playback only, not the file)")
        self._volume_slider.valueChanged.connect(self._on_volume_changed)

        controls = QHBoxLayout()
        controls.setContentsMargins(6, 4, 6, 4)
        controls.setSpacing(8)
        controls.addWidget(self._play_btn)
        controls.addWidget(self._time_label)
        controls.addStretch(1)
        controls.addWidget(self._mute_btn)
        controls.addWidget(self._volume_slider)

        # --- layout ---
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._video_stack, stretch=1)
        root.addLayout(controls)

        # --- wiring ---
        self._player.positionChanged.connect(self._on_player_position)
        self._player.durationChanged.connect(self._on_player_duration)
        self._player.playbackStateChanged.connect(self._on_player_state)
        self._player.errorOccurred.connect(self._on_player_error)

        # Nothing's loaded yet — keep the controls inert until load(path).
        self._set_controls_enabled(False)

    # ------------------------------------------------------------------ API
    def load(self, path: Path | str | None) -> None:
        """Load a recording. Pass None to clear + disable the controls."""
        if path is None:
            self._current_path = None
            self._player.stop()
            self._player.setSource(QUrl())
            self._time_label.setText("0:00 / 0:00")
            self._set_controls_enabled(False)
            self._prime_pending = False
            self._duration_hint_ms = 0
            self._pause_at_seconds = None
            self._video_stack.setCurrentIndex(1)  # empty-state placeholder
            return
        p = Path(path)
        self._current_path = p
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(p)))
        self._set_controls_enabled(True)
        self._video_stack.setCurrentIndex(0)  # video widget
        # Request first-frame prime; durationChanged will fire once metadata
        # is parsed and we'll briefly play→pause to paint the first frame.
        self._prime_pending = True
        # Any externally-supplied duration hint from a previous clip is
        # invalid for the new one; the editor will call set_duration_hint
        # again once ffprobe finishes.
        self._duration_hint_ms = 0
        self._pause_at_seconds = None

    def set_duration_hint_seconds(self, seconds: float | None) -> None:
        """Apply an externally-probed duration (ffprobe).

        Used as a fallback when QMediaPlayer reports duration=0 for a
        recording whose container metadata is incomplete. Passing None or
        a non-positive value clears the hint.
        """
        ms = int(round(seconds * 1000)) if seconds and seconds > 0 else 0
        if ms == self._duration_hint_ms:
            return
        self._duration_hint_ms = ms
        self._sync_duration_ui()

    def _effective_duration_ms(self) -> int:
        """Whichever's bigger: QMediaPlayer's or the ffprobe hint."""
        return max(int(self._player.duration() or 0), self._duration_hint_ms)

    def _sync_duration_ui(self) -> None:
        eff = self._effective_duration_ms()
        self._update_time_label()
        self.duration_changed.emit(max(0.0, eff / 1000.0))

    def _set_controls_enabled(self, enabled: bool) -> None:
        """Toggle every interactive control in the preview together.

        Clicking Play with no source loaded was a silent no-op; users hit
        the button expecting feedback. Same for mute.
        """
        for w in (self._play_btn, self._mute_btn, self._volume_slider):
            w.setEnabled(enabled)

    def play(self) -> None:
        if self._current_path is not None:
            self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float) -> None:
        if self._current_path is None:
            return
        # User-driven seek invalidates any in-flight range preview — once
        # they move the playhead, the auto-pause endpoint no longer makes
        # sense.
        self._pause_at_seconds = None
        ms = max(0, int(round(seconds * 1000)))
        self._do_seek(ms)

    def play_range(self, start: float, end: float) -> None:
        """Seek to ``start`` and play until the playhead reaches ``end``,
        then auto-pause. Lets the user preview their trimmed clip.

        ``start`` >= ``end`` is treated as a no-op. The auto-pause is
        cleared if the user seeks during playback.
        """
        if self._current_path is None or end <= start:
            return
        start = max(0.0, start)
        end_ms = int(round(end * 1000))
        self._pause_at_seconds = None  # cleared by seek(); set after.
        self.seek(start)
        self._pause_at_seconds = end_ms / 1000.0
        self.play()

    def position(self) -> float:
        return self._player.position() / 1000.0

    def duration(self) -> float:
        return max(0.0, self._effective_duration_ms() / 1000.0)

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def toggle_mute(self) -> None:
        """Flip the mute button — used by the editor's M keyboard shortcut."""
        self._mute_btn.toggle()

    def is_fullscreen(self) -> bool:
        return self._fullscreen_host is not None

    def toggle_fullscreen(self) -> None:
        """Show / hide a top-level fullscreen video widget.

        Strategy: spawn a NEW _VideoArea as a top-level frameless window
        and switch the QMediaPlayer's videoOutput onto it. The embedded
        video widget stays in its layout the entire time — no reparenting,
        no flag-flipping mid-flight, no layout-restore bookkeeping. Exiting
        switches output back and destroys the host.
        """
        if self._fullscreen_host is None:
            self._enter_fullscreen()
        else:
            self._exit_fullscreen()

    def _enter_fullscreen(self) -> None:
        if self._fullscreen_host is not None:
            return

        host = _VideoArea(None)
        host.setWindowTitle("Momento")
        host.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        # Target the screen the EDITOR is currently displayed on, not the
        # OS primary screen — on multi-monitor setups they often differ.
        screen = self._video_widget.screen() or QApplication.primaryScreen()
        host.setStyleSheet("background-color: black;")
        # KeepAspectRatioByExpanding: aspect-matched recordings (the
        # universal case for a personal game recorder) fill the screen
        # edge-to-edge with no sub-pixel letterbox gaps from fractional
        # DPI scaling. Mismatched recordings would crop slightly at the
        # edges, which is preferable to bars for game footage where the
        # HUD sits in the centre.
        host.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        if screen is not None:
            # Pin the window to the target screen BEFORE showing it.
            host.setScreen(screen)
            host.setGeometry(screen.geometry())

        host.clicked.connect(self.toggle_play)
        host.double_clicked.connect(self.toggle_fullscreen)
        host.escape_pressed.connect(self.toggle_fullscreen)

        # The host is a separate top-level window — the editor's WindowShortcut
        # scoped F/Space/etc bindings won't fire here because the editor
        # window isn't the active one. Re-install the same shortcuts on the
        # host so playback control still works in fullscreen.
        self._install_fullscreen_shortcuts(host)

        # Mouse-tracking on the host so mouseMoveEvent fires without a
        # button pressed — that's how the overlay knows the user is
        # active.
        host.setMouseTracking(True)

        host.showFullScreen()
        host.raise_()
        host.activateWindow()
        host.setFocus(Qt.FocusReason.OtherFocusReason)
        if screen is not None:
            logger.info(
                "Fullscreen host: target screen=%s actual geometry=%s",
                screen.geometry(), host.geometry(),
            )

        self._fullscreen_host = host
        # Route the player's video output onto the new widget. The embedded
        # one becomes a passive black rect for the duration.
        self._player.setVideoOutput(host)

        # YouTube-style overlay: floating top-level window anchored over
        # the host's bottom edge. Construct AFTER showFullScreen so the
        # host has its final geometry; show LAST so the WindowStaysOnTop
        # flag takes effect against an already-shown host.
        overlay = _FullscreenOverlay(host, self)
        overlay.show()
        overlay.raise_()
        # Hand focus back to the host so its keyboard shortcuts still fire.
        host.activateWindow()
        host.setFocus(Qt.FocusReason.OtherFocusReason)
        self._fullscreen_overlay = overlay

    def _install_fullscreen_shortcuts(self, host: QWidget) -> None:
        """Wire keyboard control to the fullscreen host widget.

        Each QShortcut's parent is the host, scoped to the host's window so
        the bindings only fire while the host has focus. The shortcuts are
        garbage-collected together with the host when fullscreen exits, so
        there's no double-trigger risk after returning to embedded view.
        """
        def add(seq: str, slot) -> None:
            sc = QShortcut(QKeySequence(seq), host)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

        add("F", self.toggle_fullscreen)
        add("Space", self.toggle_play)
        add("M", self.toggle_mute)
        add("Home", lambda: self.seek(0.0))
        add("End", self._seek_to_end)
        add("Left", lambda: self._nudge(-5.0))
        add("Right", lambda: self._nudge(5.0))
        add("Shift+Left", lambda: self._nudge(-1.0))
        add("Shift+Right", lambda: self._nudge(1.0))

    def _nudge(self, seconds: float) -> None:
        dur = self.duration()
        if dur <= 0:
            return
        target = max(0.0, min(dur, self.position() + seconds))
        self.seek(target)

    def _seek_to_end(self) -> None:
        dur = self.duration()
        if dur > 0:
            self.seek(max(0.0, dur - 0.05))

    def _exit_fullscreen(self) -> None:
        host = self._fullscreen_host
        if host is None:
            return
        self._fullscreen_host = None
        overlay = self._fullscreen_overlay
        self._fullscreen_overlay = None
        # Route back to the embedded widget first, THEN tear the host
        # down — avoids a moment where the player has no video sink.
        self._player.setVideoOutput(self._video_widget)
        if overlay is not None:
            try:
                overlay.close()
                overlay.deleteLater()
            except Exception:
                logger.exception("Error tearing down fullscreen overlay")
        try:
            host.close()
            host.deleteLater()
        except Exception:
            logger.exception("Error tearing down fullscreen host")

    def _exit_fullscreen_if_active(self) -> None:
        """Escape handler — no-op when we're not actually fullscreen."""
        if self._fullscreen_host is not None:
            self._exit_fullscreen()

    # --------------------------------------------------------------- slots
    def _on_player_position(self, ms: int) -> None:
        if self._pause_at_seconds is not None:
            # Auto-pause for play_range. Use a small grace window so the
            # exact endpoint doesn't get missed between position ticks
            # (~50ms apart from QMediaPlayer at typical configs).
            if ms / 1000.0 >= self._pause_at_seconds:
                target = self._pause_at_seconds
                self._pause_at_seconds = None
                self._player.pause()
                # Park the playhead exactly on the endpoint so a follow-up
                # Export sees the trim-handle range, not a position just
                # past it.
                self._player.setPosition(int(round(target * 1000)))
        self._update_time_label()
        self.position_changed.emit(ms / 1000.0)

    def _on_player_duration(self, ms: int) -> None:
        # Defer to _sync_duration_ui so the player + hint values both feed
        # the same single source of truth.
        self._sync_duration_ui()
        # Prime the first frame so the preview isn't a black void until the
        # user hits play. Only does this once per load(), and only if the
        # user hasn't already started playback.
        if self._prime_pending and ms > 0:
            self._prime_pending = False
            if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self._player.play()
                QTimer.singleShot(80, self._prime_pause)

    def _prime_pause(self) -> None:
        """Pause from the priming play burst — but only if the user hasn't
        meanwhile clicked play themselves (in which case the position will
        have advanced past the first-frame window)."""
        if self._player.position() <= 200:
            self._player.pause()
            # Snap back to the very start so the scrubber + time label
            # don't sit at "0:00.08" after the prime.
            self._player.setPosition(0)

    def _on_player_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._set_play_icon(playing=playing)
        self.playback_state_changed.emit(playing)
        if playing:
            # Pause→Play (and Stopped→Play) transitions produce a loud
            # buffer-prime artifact: WMF wakes its audio backend with
            # stale/uninitialised PCM in the first packet. Re-use the
            # seek mute pattern to swallow the first ~150 ms.
            if not self._audio.isMuted():
                self._mute_by_seek = True
                self._audio.setMuted(True)
            if self._mute_by_seek:
                self._seek_mute_timer.start(150)

    def _on_player_error(self, error: QMediaPlayer.Error, error_string: str = "") -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        msg = error_string or self._player.errorString() or str(error)
        logger.error("QMediaPlayer error: %s", msg)
        self.error_occurred.emit(msg)

    def _do_seek(self, ms: int) -> None:
        """Set position with a brief audio mute. Idempotent — repeated calls
        during a drag keep the mute on; it lifts 150 ms after the LAST call."""
        if not self._audio.isMuted():
            self._mute_by_seek = True
            self._audio.setMuted(True)
        if self._mute_by_seek:
            self._seek_mute_timer.start(150)
        self._player.setPosition(ms)

    def _end_seek_mute(self) -> None:
        if self._mute_by_seek:
            self._audio.setMuted(False)
            self._mute_by_seek = False

    # -------------------------------------------------------- devices
    def _sync_audio_to_default(self) -> None:
        """Re-point the audio output at the current Windows default.

        Fires when devices are added/removed or when the user changes the
        default playback device in Windows Sound settings.
        """
        new_default = QMediaDevices.defaultAudioOutput()
        if new_default is None:
            return
        if bytes(new_default.id()) == bytes(self._audio.device().id()):
            return  # default didn't actually change
        self._audio.setDevice(new_default)
        logger.info("Preview audio re-routed to %r", new_default.description())

    # --------------------------------------------------------- volume
    def _on_volume_changed(self, value: int) -> None:
        vol = max(0.0, min(1.0, value / 100.0))
        self._audio.setVolume(vol)
        if value > 0 and self._mute_btn.isChecked():
            # Dragging the slider up unmutes — feels natural.
            self._mute_btn.setChecked(False)
        if value > 0:
            self._volume_before_mute = vol

    def _on_mute_toggled(self, muted: bool) -> None:
        self._audio.setMuted(muted)
        self._set_mute_icon(muted=muted)
        # User has explicit intent — drop any in-flight seek-mute so we
        # don't undo their choice when the timer fires.
        self._mute_by_seek = False
        self._seek_mute_timer.stop()

    def _set_mute_icon(self, muted: bool) -> None:
        style = self.style()
        icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        self._mute_btn.setIcon(icon)
        self._mute_btn.setToolTip("Unmute" if muted else "Mute")

    # ------------------------------------------------------------ helpers
    def _set_play_icon(self, playing: bool) -> None:
        style = self.style()
        icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        )
        self._play_btn.setIcon(icon)
        self._play_btn.setToolTip("Pause" if playing else "Play")

    def _update_time_label(self) -> None:
        cur = self._player.position() / 1000.0
        dur = max(0.0, self._effective_duration_ms() / 1000.0)
        self._time_label.setText(f"{fmt_time(cur)} / {fmt_time(dur)}")
