"""Trim-timeline widget.

A horizontal track showing the video's duration, two draggable handles
(start, end) defining the clip region, and a playhead line that tracks the
preview's current position.

Public surface used by the editor:

    set_duration(seconds)        — call when a new file loads
    set_playhead(seconds)        — call on preview position changes
    start_seconds / end_seconds  — properties, in [0, duration]

Signals:
    start_changed(seconds)       — emitted as the user drags the start handle
    end_changed(seconds)         — emitted as the user drags the end handle
    seek_requested(seconds)      — emitted when a drag should seek the preview
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from momento.ui import theme as _theme
from momento.util.time_format import fmt_time

_TRACK_HEIGHT = 28  # slim Obsidian track — keeps more vertical room for the player
_HANDLE_WIDTH = 12
_PAD_X = _HANDLE_WIDTH // 2 + 2  # leave room for handles at the edges

# Ruler layout (drawn above the track)
_RULER_LABEL_HEIGHT = 13
_RULER_MAJOR_TICK = 6
_RULER_MINOR_TICK = 3
_RULER_MIN_LABEL_PX = 64   # minimum pixels between major labels — drives interval picker
_RULER_MIN_MINOR_PX = 5    # don't draw minor ticks if they'd be denser than this

# "Nice" intervals (seconds) considered when picking the major step. Each entry
# is paired with how many minor ticks to draw between two majors — values
# chosen so the minor positions land on round numbers in the current unit
# (e.g. 5m majors -> 1m minors). Keep these in step.
_RULER_INTERVALS = (
    (1, 10), (2, 4), (5, 5), (10, 10), (15, 3), (30, 6),
    (60, 6), (120, 4), (300, 5), (600, 10), (900, 3), (1800, 6),
    (3600, 6), (7200, 4), (14400, 4), (21600, 6), (43200, 6), (86400, 4),
)


class Timeline(QWidget):
    start_changed = pyqtSignal(float)
    end_changed = pyqtSignal(float)
    seek_requested = pyqtSignal(float)
    # Distinct from seek_requested: clicking a bookmark marker is a "jump here
    # and watch" action, so the editor seeks AND starts playback — whereas a
    # plain track-click / handle-drag only seeks.
    bookmark_clicked = pyqtSignal(float)

    view_changed = pyqtSignal(float, float)  # view_start, view_end

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(72)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._duration = 0.0
        self._start = 0.0
        self._end = 0.0
        self._playhead = 0.0
        self._bookmarks: list[float] = []

        # 'start' | 'end' | None — which handle the mouse is currently dragging.
        self._dragging: str | None = None
        # Hover state for cursor cue.
        self._hover: str | None = None

        # Zoom: the visible time range. Mouse wheel zooms centered on the
        # cursor; the editor's scrollbar pans within [0, duration]. When
        # view_start == 0 and view_end == duration we render the full clip.
        self._view_start = 0.0
        self._view_end = 0.0
        # Minimum zoom window so the user can't zoom to a degenerate range
        # they can't click out of. 0.5s covers a few keyframes at any sane
        # framerate, which is the trim resolution anyway.
        self._min_view_range = 0.5

    # ---------------------------------------------------------- public API
    def set_duration(self, seconds: float) -> None:
        """Set the media duration.

        The editor calls this with 0 when the selection changes (explicit
        "new clip" reset), then the real duration arrives via the preview's
        duration_changed signal. QMediaPlayer's WMF backend re-emits
        durationChanged freely after that — at playback start, on pause, and
        whenever it refines its estimate (and the ffprobe hint lands as
        another emission) — so a same-clip update must PRESERVE the user's
        trim handles, zoom and playhead. Resetting on every emission was the
        "drag the end handle, hit Play clip portion, the handle teleports
        back to the end and the trim is wiped" bug: only a fresh load (from
        duration 0) may reset state.
        """
        seconds = max(0.0, float(seconds))
        if abs(seconds - self._duration) < 1e-6:
            return  # duplicate re-emission — nothing changed
        old_duration = self._duration
        self._duration = seconds
        if old_duration <= 0.0 or seconds <= 0.0:
            # Fresh load (or selection cleared) — full reset.
            self._start = 0.0
            self._end = seconds
            self._playhead = 0.0
            self._view_start = 0.0
            self._view_end = seconds
            self.view_changed.emit(self._view_start, self._view_end)
            self.update()
            return
        # Same-clip duration refinement: keep the user's state, clamped to
        # the new duration. An untrimmed end (== the old duration) follows
        # the refinement so "whole clip" stays whole.
        end_was_full = self._end >= old_duration - 1e-6
        view_was_full = (
            self._view_start <= 1e-6 and self._view_end >= old_duration - 1e-6
        )
        self._end = seconds if end_was_full else min(self._end, seconds)
        self._start = max(0.0, min(self._start, self._end - 0.05))
        self._playhead = min(self._playhead, seconds)
        if view_was_full:
            new_view = (0.0, seconds)
        else:
            view_end = min(self._view_end, seconds)
            view_start = max(0.0, min(self._view_start, view_end - self._min_view_range))
            new_view = (view_start, view_end)
        self._view_start, self._view_end = new_view
        # Emit even when the visible range itself didn't move: the editor's
        # pan scrollbar derives its range from view + DURATION, and the
        # duration just changed — without this the scrollbar goes stale when
        # a refinement lands while zoomed in.
        self.view_changed.emit(self._view_start, self._view_end)
        self.update()

    @property
    def view_start(self) -> float:
        return self._view_start

    @property
    def view_end(self) -> float:
        return self._view_end

    def set_view(self, view_start: float, view_end: float) -> None:
        """Set the visible time range. Clamps to ``[0, duration]`` and to
        the minimum zoom window."""
        if self._duration <= 0:
            return
        view_start = max(0.0, min(self._duration, float(view_start)))
        view_end = max(0.0, min(self._duration, float(view_end)))
        if view_end - view_start < self._min_view_range:
            view_end = min(self._duration, view_start + self._min_view_range)
            view_start = max(0.0, view_end - self._min_view_range)
        if view_start == self._view_start and view_end == self._view_end:
            return
        self._view_start = view_start
        self._view_end = view_end
        self.view_changed.emit(view_start, view_end)
        self.update()

    def reset_zoom(self) -> None:
        self.set_view(0.0, self._duration)

    def set_playhead(self, seconds: float) -> None:
        self._playhead = max(0.0, min(self._duration, float(seconds)))
        self.update()

    def set_bookmarks(self, bookmarks: list[float] | None) -> None:
        self._bookmarks = sorted(float(b) for b in (bookmarks or []))
        self.update()

    def set_clip_range(self, start: float, end: float) -> None:
        """Programmatic equivalent of dragging both handles. Clamps to
        ``[0, duration]`` and emits the change signals. ``end`` is forced
        to be at least 0.05 s after ``start`` to match the drag invariant."""
        if self._duration <= 0:
            return
        start = max(0.0, min(self._duration, float(start)))
        end = max(0.0, min(self._duration, float(end)))
        if end - start < 0.05:
            end = min(self._duration, start + 0.05)
        if start == self._start and end == self._end:
            return
        self._start = start
        self._end = end
        self.update()
        self.start_changed.emit(self._start)
        self.end_changed.emit(self._end)

    @property
    def start_seconds(self) -> float:
        return self._start

    @property
    def end_seconds(self) -> float:
        return self._end

    @property
    def duration(self) -> float:
        return self._duration

    # ----------------------------------------------------- coordinate math
    def _track_rect(self) -> QRectF:
        w = max(0, self.width() - 2 * _PAD_X)
        # The ruler occupies the top strip; offset the track below it. Fixed
        # placement (rather than centering) keeps the ruler-to-track gap
        # consistent across widget heights.
        y = _RULER_LABEL_HEIGHT + _RULER_MAJOR_TICK + 4
        return QRectF(_PAD_X, y, w, _TRACK_HEIGHT)

    def _seconds_to_x(self, seconds: float) -> float:
        track = self._track_rect()
        view_range = self._view_end - self._view_start
        if view_range <= 0 or track.width() <= 0:
            return float(track.left())
        ratio = (seconds - self._view_start) / view_range
        # Unclamped on purpose: callers (handle / playhead drawing) check
        # the result against the track rect themselves so off-view markers
        # can be skipped cleanly.
        return float(track.left() + ratio * track.width())

    def _x_to_seconds(self, x: float) -> float:
        track = self._track_rect()
        view_range = self._view_end - self._view_start
        if track.width() <= 0 or view_range <= 0:
            return self._view_start
        ratio = (x - track.left()) / track.width()
        return max(0.0, min(self._duration, self._view_start + ratio * view_range))

    def _handle_at(self, pos_x: float) -> str | None:
        """Return 'start' or 'end' if pos_x is over a handle."""
        if self._duration <= 0:
            return None
        sx = self._seconds_to_x(self._start)
        ex = self._seconds_to_x(self._end)
        # Hit-test with a forgiving half-width.
        slop = _HANDLE_WIDTH
        near_start = abs(pos_x - sx) <= slop
        near_end = abs(pos_x - ex) <= slop
        if near_start and near_end:
            # Handles overlap (e.g. "Set start here" near the end, or a
            # minimum-length selection). Always answering 'start' made the
            # end handle un-grabbable — disambiguate by which side of the
            # pair the press landed on.
            return "start" if pos_x <= (sx + ex) / 2 else "end"
        if near_start:
            return "start"
        if near_end:
            return "end"
        return None

    def _bookmark_at(self, pos_x: float, pos_y: float) -> float | None:
        """Return the bookmark second-value near pos, or None."""
        if not self._bookmarks or self._duration <= 0:
            return None
        track = self._track_rect()
        # Bookmark hit zone: just below the track (where ticks are drawn).
        if pos_y < track.bottom() or pos_y > track.bottom() + 10:
            return None
        for bm in self._bookmarks:
            bx = self._seconds_to_x(bm)
            if abs(pos_x - bx) <= 5:
                return bm
        return None

    # -------------------------------------------------------------- paint
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        track = self._track_rect()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Time ruler above the track — labels + major/minor ticks. Drawn
            # before the track so the playhead line ends up on top of it.
            self._draw_ruler(painter, track)

            # Track background — Obsidian input surface + hairline border.
            painter.setPen(QPen(QColor(_theme.BORDER), 1))
            painter.setBrush(QColor(_theme.BG_INPUT))
            painter.drawRoundedRect(track, 10, 10)

            if self._duration > 0:
                sx = self._seconds_to_x(self._start)
                ex = self._seconds_to_x(self._end)
                sel_left = max(sx, track.left())
                sel_right = min(ex, track.right())

                # Dim the regions OUTSIDE the trim so the kept range pops.
                dim = QColor(8, 8, 11, 168)
                painter.setPen(Qt.PenStyle.NoPen)
                if sel_left > track.left():
                    painter.setBrush(dim)
                    painter.drawRoundedRect(
                        QRectF(track.left(), track.top(),
                               sel_left - track.left(), track.height()), 10, 10)
                if sel_right < track.right():
                    painter.setBrush(dim)
                    painter.drawRoundedRect(
                        QRectF(sel_right, track.top(),
                               track.right() - sel_right, track.height()), 10, 10)

                # Selected (clip) region — soft violet→magenta wash.
                if sel_right > sel_left:
                    sel = QRectF(
                        sel_left, track.top(),
                        sel_right - sel_left, track.height(),
                    )
                    wash = QLinearGradient(sel.topLeft(), sel.bottomLeft())
                    c0 = QColor(168, 85, 247)
                    c0.setAlpha(64)
                    c1 = QColor(217, 70, 239)
                    c1.setAlpha(30)
                    wash.setColorAt(0.0, c0)
                    wash.setColorAt(1.0, c1)
                    painter.setBrush(wash)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawRect(sel)

                # Bookmark "highlight" pips — glowing violet rounded squares
                # sitting just below the track (the click hit-zone).
                if self._bookmarks:
                    for bm in self._bookmarks:
                        if not (self._view_start <= bm <= self._view_end):
                            continue
                        bx = self._seconds_to_x(bm)
                        self._draw_pip(painter, bx, track.bottom() + 5)

                # Playhead — white line + glow + dot cap, inside the view only.
                ph_x = self._seconds_to_x(self._playhead)
                if track.left() <= ph_x <= track.right():
                    glow = QColor(255, 255, 255, 70)
                    painter.setPen(QPen(glow, 5))
                    painter.drawLine(int(ph_x), int(track.top() - 4),
                                     int(ph_x), int(track.bottom() + 4))
                    painter.setPen(QPen(QColor(255, 255, 255), 2))
                    painter.drawLine(int(ph_x), int(track.top() - 4),
                                     int(ph_x), int(track.bottom() + 4))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(255, 255, 255))
                    painter.drawEllipse(QPointF(ph_x, track.top() - 4), 4.0, 4.0)

                # Handles — skip drawing when off-view (still draggable from
                # the visible edge via the off-view affordance the editor
                # surfaces via the scrollbar).
                if track.left() - _HANDLE_WIDTH <= sx <= track.right() + _HANDLE_WIDTH:
                    self._draw_handle(painter, sx, "start", track)
                if track.left() - _HANDLE_WIDTH <= ex <= track.right() + _HANDLE_WIDTH:
                    self._draw_handle(painter, ex, "end", track)

                # Time labels under handles — show the exact trim positions
                # of the START and END of the current selection (not the
                # view edges). When zoomed past a handle, the label sticks
                # to the nearest edge of the track so the user can still
                # read the selected range.
                painter.setPen(QColor(_theme.TEXT_FAINT))
                font = painter.font()
                font.setPointSize(8)
                painter.setFont(font)
                start_label_x = max(int(track.left()), int(min(sx, track.right())))
                painter.drawText(
                    start_label_x, int(track.bottom() + 18),
                    fmt_time(self._start),
                )
                end_text = fmt_time(self._end)
                metrics = painter.fontMetrics()
                end_w = metrics.horizontalAdvance(end_text)
                end_label_x = min(
                    int(track.right() - end_w),
                    max(int(track.left()), int(ex - end_w)),
                )
                painter.drawText(
                    end_label_x, int(track.bottom() + 18),
                    end_text,
                )
        finally:
            painter.end()

    def _draw_ruler(self, painter: QPainter, track: QRectF) -> None:
        view_range = self._view_end - self._view_start
        if view_range <= 0 or track.width() <= 0:
            return

        major_secs, minor_count = _pick_ruler_interval(
            view_range, track.width()
        )
        if major_secs <= 0:
            return

        # Label format follows the full clip duration, not the view range,
        # so a hour-plus clip keeps H:MM:SS labels even when zoomed in
        # under an hour.
        show_hours = self._duration >= 3600
        font = QFont(painter.font())
        font.setPointSize(7)
        painter.setFont(font)
        fm = QFontMetrics(font)
        baseline = fm.ascent() + 1

        tick_top = _RULER_LABEL_HEIGHT + 1
        major_bottom = tick_top + _RULER_MAJOR_TICK
        minor_bottom = tick_top + _RULER_MINOR_TICK

        px_per_second = track.width() / view_range
        minor_secs = major_secs / minor_count if minor_count > 0 else 0.0
        draw_minors = (
            minor_secs > 0 and minor_secs * px_per_second >= _RULER_MIN_MINOR_PX
        )

        # Hoist the linear-interp factors out of the tick loop. With dense
        # ticks (zoomed-in long clip) the loop runs hundreds of iterations
        # per repaint, so the per-call _seconds_to_x → _track_rect path adds
        # up.
        import math
        track_left = float(track.left())
        track_right = float(track.right())

        def x_for(t: float) -> float:
            return track_left + (t - self._view_start) * px_per_second

        first_major_n = math.floor(self._view_start / major_secs)
        last_major_n = math.ceil(self._view_end / major_secs)

        if draw_minors:
            painter.setPen(QPen(QColor("#2a2c34"), 1))
            n = first_major_n * minor_count
            last_minor_n = last_major_n * minor_count
            while n <= last_minor_n:
                if n % minor_count != 0:
                    t = n * minor_secs
                    if 0.0 <= t <= self._duration + 1e-6:
                        x = x_for(t)
                        if track_left - 1 <= x <= track_right + 1:
                            painter.drawLine(int(x), tick_top, int(x), minor_bottom)
                n += 1

        major_pen = QPen(QColor("#3a3d47"), 1)
        label_color = QColor(_theme.TEXT_FAINT)
        widget_right = self.width()
        last_label_right = -1
        for n in range(first_major_n, last_major_n + 1):
            t = n * major_secs
            if t < -1e-6 or t > self._duration + 1e-6:
                continue
            x = x_for(t)
            if not (track_left - 1 <= x <= track_right + 1):
                continue
            painter.setPen(major_pen)
            painter.drawLine(int(x), tick_top, int(x), major_bottom)
            label = fmt_time(t, force_hours=show_hours)
            label_w = fm.horizontalAdvance(label)
            label_x = int(x - label_w / 2)
            label_x = max(0, min(label_x, widget_right - label_w))
            if label_x <= last_label_right + 4:
                continue  # would visually crowd the previous label
            painter.setPen(label_color)
            painter.drawText(label_x, baseline, label)
            last_label_right = label_x + label_w

    def _draw_pip(self, painter: QPainter, cx: float, cy: float) -> None:
        """A glowing violet 'highlight' marker (rounded square)."""
        glow = QColor(_theme.ACCENT_VIOLET)
        glow.setAlpha(90)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawRoundedRect(QRectF(cx - 5.5, cy - 5.5, 11, 11), 4, 4)
        painter.setBrush(QColor(_theme.ACCENT_VIOLET))
        painter.drawRoundedRect(QRectF(cx - 4, cy - 4, 8, 8), 3, 3)

    def _draw_handle(self, painter: QPainter, x: float, kind: str, track: QRectF) -> None:
        hot = self._hover == kind or self._dragging == kind
        base = _theme.ACCENT_VIOLET if kind == "start" else _theme.ACCENT_MAGENTA
        color = QColor(base)
        if hot:
            color = color.lighter(115)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        rect = QRectF(
            x - _HANDLE_WIDTH / 2,
            track.top() - 4,
            _HANDLE_WIDTH,
            track.height() + 8,
        )
        painter.drawRoundedRect(rect, 4, 4)
        # central grip line
        painter.setPen(QPen(QColor(0, 0, 0, 110), 2))
        gy0 = rect.center().y() - 8
        gy1 = rect.center().y() + 8
        painter.drawLine(int(rect.center().x()), int(gy0), int(rect.center().x()), int(gy1))

    # -------------------------------------------------------------- input
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._duration <= 0:
            return
        pos = event.position()
        # 1) Bookmark tick — the pips live in their own band BELOW the track
        # (enforced by _bookmark_at's Y check), so a press there is
        # unambiguously a bookmark click. Testing handles first made any pip
        # within a handle's slop-width unclickable (the press moved the trim
        # handle instead).
        bookmark = self._bookmark_at(pos.x(), pos.y())
        if bookmark is not None:
            self.bookmark_clicked.emit(bookmark)
            return
        # 2) Handle drag — the handles sit on top of the track itself.
        which = self._handle_at(pos.x())
        if which is not None:
            self._dragging = which
            self._apply_drag(pos.x())
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        # 3) Anywhere else within the timeline's vertical extent counts as a
        # seek to that horizontal position. Users expect to drop the playhead
        # by clicking the bar (especially "rewind just before the bookmark").
        track = self._track_rect()
        if track.left() <= pos.x() <= track.right():
            self.seek_requested.emit(self._x_to_seconds(pos.x()))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging is not None:
            self._apply_drag(event.position().x())
            return
        pos = event.position()
        new_hover = self._handle_at(pos.x())
        if new_hover != self._hover:
            self._hover = new_hover
            self.update()
        # Determine the appropriate cursor + tooltip for this hover position.
        bm = self._bookmark_at(pos.x(), pos.y())
        track = self._track_rect()
        on_track = track.left() <= pos.x() <= track.right()
        if bm is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Bookmark @ {fmt_time(bm)} — click to seek")
        elif new_hover is not None:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.setToolTip("")
        elif on_track and self._duration > 0:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            seconds = self._x_to_seconds(pos.x())
            self.setToolTip(f"Seek to {fmt_time(seconds)}")
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip("")

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        self._dragging = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._duration <= 0:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        # Zoom centered on the cursor: the time under the cursor stays at
        # the same screen x after the zoom. Standard pattern for any
        # scrubbable-timeline UI.
        factor = (1.0 / 1.25) if delta > 0 else 1.25
        track = self._track_rect()
        cursor_x = event.position().x()
        if track.width() <= 0:
            event.ignore()
            return
        cursor_seconds = self._x_to_seconds(cursor_x)
        cur_range = self._view_end - self._view_start
        new_range = max(self._min_view_range, min(self._duration, cur_range * factor))
        ratio = (cursor_x - track.left()) / track.width()
        ratio = max(0.0, min(1.0, ratio))
        new_start = cursor_seconds - ratio * new_range
        new_end = new_start + new_range
        # Clamp to [0, duration] while preserving the range size. Order
        # matters: shift right first, then left, then final clamp so both
        # ends sit inside the valid range even when the range is wider than
        # one of the gaps.
        if new_start < 0:
            new_end += -new_start
            new_start = 0
        if new_end > self._duration:
            new_start -= (new_end - self._duration)
            new_end = self._duration
        new_start = max(0.0, new_start)
        new_end = min(self._duration, new_end)
        self.set_view(new_start, new_end)
        event.accept()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._hover = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _apply_drag(self, x: float) -> None:
        seconds = self._x_to_seconds(x)
        if self._dragging == "start":
            self._start = min(seconds, self._end - 0.05)
            self._start = max(0.0, self._start)
            self.start_changed.emit(self._start)
            self.seek_requested.emit(self._start)
        elif self._dragging == "end":
            self._end = max(seconds, self._start + 0.05)
            self._end = min(self._duration, self._end)
            self.end_changed.emit(self._end)
            self.seek_requested.emit(self._end)
        self.update()


def _pick_ruler_interval(duration: float, available_px: float) -> tuple[float, int]:
    """Return (major_interval_seconds, minor_ticks_per_major) for the ruler.

    Picks the smallest "nice" interval whose pixel spacing is at least
    ``_RULER_MIN_LABEL_PX`` — guarantees labels never overlap regardless
    of clip length, while keeping the densest possible labelling for short
    clips so you can scrub by the second.
    """
    if duration <= 0 or available_px <= 0:
        return 0.0, 0
    seconds_per_pixel = duration / available_px
    min_interval = _RULER_MIN_LABEL_PX * seconds_per_pixel
    for interval, minors in _RULER_INTERVALS:
        if interval >= min_interval:
            return float(interval), minors
    return float(_RULER_INTERVALS[-1][0]), _RULER_INTERVALS[-1][1]
