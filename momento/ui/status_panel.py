"""Live recording-status strip shown at the top of the editor window.

Surfaces what Momento is doing right now — whether it's recording, which
game, how long for — plus quick-glance environment checks (mic configured,
system audio configured, free disk space). Driven from the tray's
session-status signal and a 1 s QTimer that polls the recorder + filesystem.

Obsidian redesign: the strip is the window's dark chrome (``bg/chrome``). It
carries the brand mark + a Settings entry and the state pill on the left, and
Mic / System audio / Free space chips on the right.

Only the tray builds it with a real ``SessionManager``; the editor exposes it so
the tray can push status. ``set_status`` / ``set_config`` and the ``_tick`` poll
keep their original data contract — only the presentation changed.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from momento.config import Config
from momento.core.audio_loopback import resolve_loopback_device
from momento.core.game_names import humanise_game_name
from momento.core.game_watcher import ActiveGame
from momento.core.mic_capture import resolve_mic_device
from momento.core.session import STATUS_RECORDING, SessionManager
from momento.ui import icons, theme
from momento.util.format import format_bytes
from momento.util.time_format import fmt_time

# Pill palettes: (bg, border, text, subtext, dot QColor).
_PILL_RECORDING = ("rgba(239,68,68,0.10)", "rgba(239,68,68,0.32)", "#fb7185", "#9a4b50", QColor(239, 68, 68))
_PILL_MONITORING = ("rgba(34,197,94,0.10)", "rgba(34,197,94,0.28)", "#4ade80", "#2f7d4d", QColor(34, 197, 94))
_PILL_IDLE = ("#14151b", "#23242e", "#b6b8c2", "#6b6d78", QColor(120, 124, 136))


class _PulseDot(QWidget):
    """A small glowing status dot that breathes (opacity + scale) like the
    design's monitoring indicator."""

    def __init__(self, diameter: int = 9, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._d = diameter
        self._colour = QColor(34, 197, 94)
        self._phase = 0.0
        self.setFixedSize(diameter + 6, diameter + 6)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(50)  # ~20 fps
        self._timer.timeout.connect(self._advance)

    def set_colour(self, c: QColor) -> None:
        self._colour = c
        self.update()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        self._phase = (self._phase + 0.05) % 1.0  # 1.0s loop at 20fps
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        pulse = 0.5 - 0.5 * math.cos(2 * math.pi * self._phase)  # 0→1→0
        opacity = 1.0 - 0.6 * pulse
        scale = 1.0 - 0.18 * pulse
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            cx, cy = self.width() / 2, self.height() / 2
            r = (self._d / 2) * scale
            # soft glow
            glow = QColor(self._colour)
            glow.setAlphaF(0.35 * opacity)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(int(cx - r - 2.5), int(cy - r - 2.5), int((r + 2.5) * 2), int((r + 2.5) * 2))
            dot = QColor(self._colour)
            dot.setAlphaF(opacity)
            p.setBrush(dot)
            p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        finally:
            p.end()


def _chip(*children: QWidget, raised: bool = True, pad: tuple[int, int] = (12, 7)) -> QFrame:
    """A rounded status chip holding the given child widgets in a row."""
    f = QFrame()
    f.setObjectName("StatusChip")
    bg = theme.BG_CARD_RAISED if raised else "transparent"
    f.setStyleSheet(
        f"QFrame#StatusChip {{ background: {bg}; border: 1px solid {theme.BORDER}; "
        f"border-radius: 9px; }}"
        f"QFrame#StatusChip QLabel {{ background: transparent; }}"
    )
    lay = QHBoxLayout(f)
    lay.setContentsMargins(pad[0], pad[1], pad[0], pad[1])
    lay.setSpacing(7)
    for c in children:
        lay.addWidget(c)
    return f


def _chip_label(text: str, colour: str, size: str = "12px", weight: int = 600) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {colour}; font-size: {size}; font-weight: {weight};")
    return lbl


class _StatePill(QFrame):
    """The primary state pill: glowing dot + 'Monitoring' / 'Recording' label +
    a muted subline (detected/active game)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatePill")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(13, 7, 14, 7)
        lay.setSpacing(9)
        self._dot = _PulseDot(8)
        lay.addWidget(self._dot)
        self._label = QLabel("Monitoring")
        lay.addWidget(self._label)
        self._sub = QLabel("")
        lay.addWidget(self._sub)

    def set_state(self, label: str, sub: str, palette) -> None:
        bg, border, fg, sub_fg, dot = palette
        self._dot.set_colour(dot)
        self._dot.start()
        self._label.setText(label)
        self._sub.setText(sub)
        self._sub.setVisible(bool(sub))
        self.setStyleSheet(
            f"QFrame#StatePill {{ background: {bg}; border: 1px solid {border}; "
            f"border-radius: 9px; }}"
            f"QFrame#StatePill QLabel {{ background: transparent; }}"
        )
        self._label.setStyleSheet(f"color: {fg}; font-weight: 700; font-size: 12.5px;")
        self._sub.setStyleSheet(f"color: {sub_fg}; font-size: 12px;")


class StatusPanel(QFrame):
    """A single-row status strip shown above the editor's main content."""

    settings_clicked = pyqtSignal()

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

        self.setObjectName("StatusPanel")
        self.setStyleSheet(
            f"QFrame#StatusPanel {{ background-color: {theme.BG_CHROME}; "
            f"border-bottom: 1px solid {theme.BORDER_HAIRLINE}; }}"
            f"QFrame#StatusPanel QLabel {{ background: transparent; }}"
        )
        self.setFixedHeight(54)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 18, 0)
        row.setSpacing(10)

        # ----- brand cluster (logo + wordmark + Settings entry) -----
        row.addWidget(self._build_brand())

        sep = QFrame()
        sep.setFixedSize(1, 22)
        sep.setStyleSheet(f"background: {theme.BORDER_HAIRLINE};")
        row.addWidget(sep)

        # ----- state pill + levels -----
        self._pill = _StatePill()
        row.addWidget(self._pill)

        self._duration_label = _chip_label("", theme.TEXT_HIGH, "12px", 700)
        # Fixed width (fits up to H:MM:SS) so the ticking elapsed time can't
        # change width each second and shift the layout to its right.
        self._duration_label.setFixedWidth(64)
        self._duration_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self._duration_label)

        row.addStretch(1)

        # ----- right chips: mic / system audio / free space -----
        self._mic_icon = icons.label("mic", 16, theme.ACCENT_VIOLET)
        self._mic_text = _chip_label("Mic", theme.TEXT_MID)
        self._mic_chip = _chip(self._mic_icon, self._mic_text)
        row.addWidget(self._mic_chip)

        self._sys_icon = icons.label("speaker", 16, theme.ACCENT_VIOLET)
        self._sys_text = _chip_label("System audio", theme.TEXT_MID)
        self._sys_chip = _chip(self._sys_icon, self._sys_text)
        row.addWidget(self._sys_chip)

        self._disk_value = _chip_label("—", theme.TEXT_HIGH, "12px", 700)
        self._disk_bar = _FreeSpaceBar()
        disk_chip = _chip(
            _chip_label("Free space", theme.TEXT_DIM_2),
            self._disk_bar,
            self._disk_value,
        )
        row.addWidget(disk_chip)

        self._refresh_audio_labels()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()  # paint initial values

    def _build_brand(self) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(theme.logo_pixmap(24))
        logo.setFixedSize(24, 24)
        lay.addWidget(logo)
        word = QLabel("Momento")
        word.setStyleSheet(f"color: {theme.TEXT_HIGH}; font-weight: 700; font-size: 13px;")
        lay.addWidget(word)
        self._settings_entry = _SettingsEntry()
        self._settings_entry.clicked.connect(self.settings_clicked.emit)
        lay.addWidget(self._settings_entry)
        return wrap

    # ------------------------------------------------------------------ API
    def hideEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Slow the tick + stop the animation when the panel isn't visible —
        # close-to-tray and the settings page both hide it for long stretches.
        self._timer.setInterval(5000)
        self._pill._dot.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._timer.setInterval(1000)
        self._pill._dot.start()
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
    @staticmethod
    def _short_device_name(full: str, limit: int = 26) -> str:
        """Clean + truncate a device name for the chip (full name on hover)."""
        name = full.replace("  (default)", "").strip()
        if len(name) > limit:
            name = name[: limit - 1].rstrip() + "…"
        return name

    def _resolve_device_name(self, kind: str) -> str | None:
        """Friendly name of the configured mic/system device, or None when
        unset or unresolvable. Device enumeration is best-effort and only runs
        on construction + settings-save, never on the 1 s tick."""
        try:
            if kind == "mic":
                if not self._config.mic_device:
                    return None
                dev = resolve_mic_device(self._config.mic_device)
            else:
                if not self._config.system_audio_device:
                    return None
                dev = resolve_loopback_device(self._config.system_audio_device)
            return dev.name if dev is not None else None
        except Exception:  # enumeration can fail if the audio stack is busy
            return None

    def _apply_device_chip(self, text_lbl, icon_lbl, glyph, fallback, off_text):
        name = self._resolve_device_name("mic" if glyph == "mic" else "system")
        configured = bool(
            self._config.mic_device if glyph == "mic" else self._config.system_audio_device
        )
        if name:
            clean = name.replace("  (default)", "").strip()
            text_lbl.setText(self._short_device_name(clean))
            text_lbl.setToolTip(clean)
            colour = theme.ACCENT_VIOLET
            text_colour = theme.TEXT_MID
        elif configured:
            text_lbl.setText(fallback)
            text_lbl.setToolTip("")
            colour = theme.ACCENT_VIOLET
            text_colour = theme.TEXT_MID
        else:
            text_lbl.setText(off_text)
            text_lbl.setToolTip("")
            colour = theme.EVENT_LEVELUP
            text_colour = theme.EVENT_LEVELUP
        text_lbl.setStyleSheet(f"color: {text_colour}; font-size: 12px; font-weight: 600;")
        icon_lbl.setPixmap(icons.pixmap(glyph, 16, colour))

    def _refresh_audio_labels(self) -> None:
        self._apply_device_chip(self._mic_text, self._mic_icon, "mic", "Mic", "Mic off")
        self._apply_device_chip(
            self._sys_text, self._sys_icon, "speaker", "System audio", "System audio off"
        )

    def _tick(self) -> None:
        recording = (
            self._current_status == STATUS_RECORDING
            and self._current_game is not None
        )
        if recording:
            display = humanise_game_name(self._current_game.exe_name)
            self._pill.set_state("Recording", f"·  {display}", _PILL_RECORDING)
            elapsed = self._session._recorder.current_position()
            self._duration_label.setText(fmt_time(elapsed) if elapsed is not None else "")
            self._duration_label.setVisible(elapsed is not None)
        elif self._session.is_monitoring:
            self._pill.set_state("Monitoring", "·  watching for games", _PILL_MONITORING)
            self._duration_label.setText("")
            self._duration_label.setVisible(False)
        else:
            self._pill.set_state("Idle", "·  monitoring paused", _PILL_IDLE)
            self._duration_label.setText("")
            self._duration_label.setVisible(False)
        self._update_disk()

    def _update_disk(self) -> None:
        path = Path(self._config.output_folder)
        try:
            usage = shutil.disk_usage(path if path.exists() else path.anchor or path)
            self._disk_value.setText(format_bytes(usage.free))
            used_frac = 0.0 if usage.total == 0 else 1.0 - (usage.free / usage.total)
            self._disk_bar.set_fraction(used_frac)
        except OSError:
            self._disk_value.setText("—")
            self._disk_bar.set_fraction(0.0)


class _FreeSpaceBar(QWidget):
    """Mini used-space progress bar (track + violet→magenta fill)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frac = 0.0
        self.setFixedSize(54, 5)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_fraction(self, frac: float) -> None:
        self._frac = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PyQt6.QtGui import QLinearGradient

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#26272f"))
            p.drawRoundedRect(0, 0, self.width(), self.height(), 2.5, 2.5)
            w = int(self.width() * self._frac)
            if w > 0:
                grad = QLinearGradient(0, 0, self.width(), 0)
                grad.setColorAt(0.0, QColor(theme.ACCENT_VIOLET))
                grad.setColorAt(1.0, QColor(theme.ACCENT_MAGENTA))
                p.setBrush(grad)
                p.drawRoundedRect(0, 0, max(4, w), self.height(), 2.5, 2.5)
        finally:
            p.end()


class _SettingsEntry(QWidget):
    """Clickable gear + 'Settings' label in the brand cluster."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 6, 2)
        lay.setSpacing(5)
        self._icon = QLabel()
        self._icon.setFixedSize(13, 13)
        lay.addWidget(self._icon)
        self._text = QLabel("Settings")
        lay.addWidget(self._text)
        self._set_colour(theme.TEXT_MID)

    def _set_colour(self, colour: str) -> None:
        self._icon.setPixmap(icons.pixmap("gear", 13, colour))
        self._text.setStyleSheet(f"color: {colour}; font-size: 12px;")

    def enterEvent(self, _event) -> None:  # noqa: N802
        self._set_colour(theme.TEXT_HIGH)

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._set_colour(theme.TEXT_MID)

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self.clicked.emit()
