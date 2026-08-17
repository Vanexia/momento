"""Global dark theme — "Obsidian".

A sharp, premium dark gamer aesthetic built around Momento's violet→magenta
heritage. A single QSS string tunes Qt's stock widgets; bespoke widgets
(status strip, recordings cards, timeline, player) import the colour tokens +
gradient/logo helpers below and paint themselves to match.

Design tokens mirror ``design_handoff_momento_redesign`` (Direction A). The
legacy token NAMES (``BG_WINDOW``, ``ACCENT`` …) are preserved and remapped to
their Obsidian values so existing per-widget references shift automatically; the
``OBS_*`` / semantic tokens below are the canonical names for new code.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QLinearGradient,
    QPainter,
    QPalette,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QApplication

# --- Obsidian palette ------------------------------------------------------
# Surfaces (darkest → lightest)
BG_APP = "#0a0b0e"           # app body / right player column
BG_WINDOW = "#0c0d11"        # window base
BG_CHROME = "#0e0f14"        # titlebar / status strip
BG_RAIL = "#0b0c10"          # left library rail, settings nav rail
BG_CARD = "#101116"          # recording cards, settings cards
BG_CARD_RAISED = "#14151b"   # status chips, secondary buttons base
BG_INPUT = "#121319"         # search field, dropdowns, value boxes
BG_FOOTER = "#0d0e13"        # export bar, settings footer
BG_HOVER = "#1a1b22"         # secondary-button / surface hover base
BG_PRESS = "#21222b"         # secondary-button pressed

# Borders (subtle → strong)
BORDER_HAIRLINE = "#1b1c24"  # chrome dividers
BORDER_SUBTLE = "#1f202a"    # card borders
BORDER = "#23242e"           # inputs, chips, slider tracks (border/default)
BORDER_HOVER = "#2c2d38"     # hover border, secondary button border

# Text
TEXT_PRIMARY = "#ffffff"     # titles, key values
TEXT = "#e6e7ee"             # high — card titles, button labels
TEXT_HIGH = "#d6d7e0"
TEXT_MID = "#9a9ca8"         # secondary labels
TEXT_DIM = "#9a9ca8"
TEXT_LOW = "#7d8088"         # meta text
TEXT_DIM_2 = "#6b6d78"
TEXT_FAINT = "#5a5c66"       # ruler ticks, captions
SETTINGS_GROUP_LABEL = "#858893"

# Accent — violet → magenta
ACCENT = "#a78bfa"           # primary accent (icons, slider start, handles)
ACCENT_VIOLET = "#a78bfa"
ACCENT_MAGENTA = "#d946ef"
ACCENT_VIOLET_DEEP = "#7c3aed"
ACCENT_VIOLET_TINT = "#c4a8f5"
ACCENT_SELECTION = "#7c3aed"  # selection-bg where white text must stay legible

# Gradient stops (consumed by the helpers below + QSS)
GRAD_PRIMARY = ("#8b3df0", "#c026d3")   # primary buttons (180deg)
GRAD_PRIMARY_HOVER = ("#9a52f3", "#d030e4")
GRAD_LOGO = ("#7c3aed", "#d946ef")      # logo squircle (150deg)
GRAD_SLIDER = ("#a78bfa", "#d946ef")    # slider fill / level-meter bars (90deg)

# Event markers
EVENT_KILL = "#ef4444"
EVENT_LEVELUP = "#fbbf24"
SPARK = "#fde047"            # logo spark + level-up logo accent

# Typography
FONT_FAMILY = "Plus Jakarta Sans"
_FONT_STACK = '"Plus Jakarta Sans", "Segoe UI", "Inter", sans-serif'

_fonts_loaded = False


def load_fonts() -> str:
    """Register the bundled Plus Jakarta Sans weights with Qt.

    Idempotent. Returns the resolved family name (falls back to Segoe UI when
    the font files aren't present, e.g. a partial dev checkout).
    """
    global _fonts_loaded
    if _fonts_loaded:
        return FONT_FAMILY
    try:
        from momento.util.resources import resources_dir

        fonts_dir = resources_dir() / "fonts"
        loaded_any = False
        if fonts_dir.is_dir():
            for ttf in sorted(fonts_dir.glob("PlusJakartaSans-*.ttf")):
                if QFontDatabase.addApplicationFont(str(ttf)) != -1:
                    loaded_any = True
        _fonts_loaded = True
        if loaded_any and FONT_FAMILY in QFontDatabase.families():
            return FONT_FAMILY
    except Exception:  # pragma: no cover — never let theming break startup
        _fonts_loaded = True
    return "Segoe UI"


# --- gradient + brand helpers ---------------------------------------------
def _grad(stops: tuple[str, str], p1: QPointF, p2: QPointF) -> QLinearGradient:
    g = QLinearGradient(p1, p2)
    g.setColorAt(0.0, QColor(stops[0]))
    g.setColorAt(1.0, QColor(stops[1]))
    return g


def logo_pixmap(
    size: int, device_ratio: float = 1.0, margin_frac: float = 0.02
) -> QPixmap:
    """Render the Momento brand squircle (violet→magenta + white M + spark).

    Reproduces ``assets/Momento Logo.svg`` (viewBox 0 0 48 48) at any size, so
    the in-app brand mark stays crisp at every DPI without shipping rasters.

    ``margin_frac`` is the transparent padding around the squircle as a fraction
    of the canvas. The design asset frames the squircle with ~4% padding, which
    reads small as a Windows taskbar icon next to apps that fill the slot — so
    the default here is a tighter 2% so the mark fills more of the bitmap.
    """
    px = max(1, int(round(size * device_ratio)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(device_ratio)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(device_ratio, device_ratio)  # work in logical px (0..size)
        # Map the squircle's design span (units 2..46) onto [m, size-m] so it
        # fills the canvas minus a small margin, then draw in DESIGN UNITS.
        m = size * margin_frac
        scale = (size - 2 * m) / 44.0
        p.translate(m - 2 * scale, m - 2 * scale)
        p.scale(scale, scale)
        # squircle
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_grad(GRAD_LOGO, QPointF(4, 4), QPointF(44, 44)))
        p.drawRoundedRect(QRectF(2, 2, 44, 44), 12, 12)
        # white "M" polygon
        pts = [
            (11, 35), (11, 14), (18, 14), (24, 23), (30, 14), (37, 14),
            (37, 35), (30, 35), (30, 25), (24, 33), (18, 25), (18, 35),
        ]
        p.setBrush(QColor("#ffffff"))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
        # yellow spark
        p.setBrush(QColor(SPARK))
        p.drawEllipse(QPointF(38, 12), 4, 4)
    finally:
        p.end()
    return pm


# --- global stylesheet -----------------------------------------------------
_QSS = f"""
* {{
    color: {TEXT};
    font-family: {_FONT_STACK};
    font-size: 10pt;
}}

QMainWindow, QDialog, QWidget#centralwidget {{
    background-color: {BG_WINDOW};
}}

QWidget {{
    background-color: transparent;
    selection-background-color: {ACCENT_SELECTION};
    selection-color: white;
}}

QFrame[frameShape="4"],   /* HLine */
QFrame[frameShape="5"] {{  /* VLine */
    color: {BORDER_HAIRLINE};
}}

QSplitter::handle {{
    background: {BORDER_HAIRLINE};
    width: 1px;
    height: 1px;
}}

/* ----- Menus (native File menu kept) ----- */
QMenuBar {{
    background-color: {BG_CHROME};
    border-bottom: 1px solid {BORDER_HAIRLINE};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
    border-radius: 6px;
    color: {TEXT_MID};
}}
QMenuBar::item:selected {{ background: {BG_HOVER}; color: {TEXT}; }}
QMenu {{
    background-color: {BG_CARD_RAISED};
    border: 1px solid {BORDER};
    padding: 5px;
    border-radius: 9px;
}}
QMenu::item {{
    padding: 7px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{ background: {ACCENT_SELECTION}; color: white; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER_HAIRLINE};
    margin: 4px 6px;
}}

/* ----- Group boxes (settings cards) ----- */
QGroupBox {{
    background: {BG_CARD};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 14px;
    margin-top: 20px;
    padding: 24px 18px 18px 18px;
    font-weight: 700;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 16px;
    padding: 0 6px;
    color: {SETTINGS_GROUP_LABEL};
    font-weight: 700;
    background: transparent;
    text-transform: uppercase;
    font-size: 8pt;
    letter-spacing: 1px;
}}

QTabWidget::pane {{
    border: none;
    background: transparent;
    margin-top: 4px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MID};
    padding: 8px 18px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QTabBar::tab:hover {{
    color: {TEXT};
    background: {BG_CARD};
}}
QTabBar::tab:selected {{
    color: {TEXT};
    background: {BG_CARD};
    border-color: {BORDER_SUBTLE};
    border-bottom: 1px solid {BG_CARD};
}}

/* ----- Buttons (secondary by default) ----- */
QPushButton {{
    background-color: {BG_HOVER};
    border: 1px solid {BORDER_HOVER};
    border-radius: 10px;
    padding: 8px 16px;
    min-height: 20px;
    color: {TEXT_HIGH};
    font-weight: 700;
}}
QPushButton:hover {{ background-color: {BG_PRESS}; border-color: #34353f; }}
QPushButton:pressed {{ background-color: #17181e; }}
QPushButton:disabled {{ color: {TEXT_DIM_2}; background-color: {BG_CARD}; border-color: {BORDER_SUBTLE}; }}

QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {GRAD_PRIMARY[0]}, stop:1 {GRAD_PRIMARY[1]});
    border: none;
    color: white;
    font-weight: 800;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {GRAD_PRIMARY_HOVER[0]}, stop:1 {GRAD_PRIMARY_HOVER[1]});
}}
QPushButton#primary:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {ACCENT_VIOLET_DEEP}, stop:1 #a31fb0);
}}
QPushButton#primary:disabled {{ background: #2a2333; color: {TEXT_DIM_2}; }}

QDialogButtonBox QPushButton[text="Save"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {GRAD_PRIMARY[0]}, stop:1 {GRAD_PRIMARY[1]});
    border: none;
    color: white;
    font-weight: 800;
}}
QDialogButtonBox QPushButton[text="Save"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {GRAD_PRIMARY_HOVER[0]}, stop:1 {GRAD_PRIMARY_HOVER[1]});
}}

/* ----- Inputs ----- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 7px 10px;
    color: {TEXT_HIGH};
    selection-background-color: {ACCENT_SELECTION};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT_VIOLET};
}}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {BORDER_HOVER};
}}
/* Inline rename editor used by tree / list / table views. */
QAbstractItemView QLineEdit {{
    padding: 0 2px;
    border-radius: 0;
    border: 1px solid {ACCENT_VIOLET};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_CARD_RAISED};
    border: 1px solid {BORDER};
    border-radius: 9px;
    selection-background-color: {ACCENT_SELECTION};
    selection-color: white;
    padding: 5px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 16px;
}}

/* ----- Check / radio ----- */
QCheckBox, QRadioButton {{ spacing: 8px; color: {TEXT_HIGH}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_HOVER};
    background: {BG_INPUT};
    border-radius: 5px;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT_VIOLET_DEEP};
    border-color: {ACCENT_VIOLET};
}}

/* ----- Sliders ----- */
QSlider::groove:horizontal {{
    height: 6px;
    background: {BORDER};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 16px;
    height: 16px;
    margin: -6px 0;
    background: #ffffff;
    border-radius: 8px;
    border: none;
}}
QSlider::handle:horizontal:hover {{ background: #f2f2f6; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {GRAD_SLIDER[0]}, stop:1 {GRAD_SLIDER[1]});
    border-radius: 3px;
}}

/* ----- Progress bar (export progress) ----- */
QProgressBar {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    text-align: center;
    color: {TEXT_MID};
    height: 18px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {GRAD_SLIDER[0]}, stop:1 {GRAD_SLIDER[1]});
    border-radius: 7px;
}}

/* ----- Item views ----- */
QHeaderView::section {{
    background-color: {BG_CARD};
    color: {TEXT_MID};
    padding: 7px 8px;
    border: none;
    border-bottom: 1px solid {BORDER_HAIRLINE};
    font-weight: 700;
}}
QTableWidget, QListWidget, QListView, QTreeView {{
    background-color: {BG_RAIL};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 10px;
    gridline-color: {BORDER_HAIRLINE};
    outline: none;
}}
QTableWidget::item, QListWidget::item, QListView::item, QTreeView::item {{
    padding: 6px 6px;
    border: none;
}}
QTableWidget::item:selected, QListWidget::item:selected,
QListView::item:selected, QTreeView::item:selected {{
    background: {ACCENT_SELECTION};
    color: white;
}}
QTableWidget::item:hover, QListWidget::item:hover,
QListView::item:hover, QTreeView::item:hover {{
    background: {BG_HOVER};
}}

/* ----- Scroll bars ----- */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px 2px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: #2a2c34;
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #3a3d47; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:horizontal {{
    background: #2a2c34;
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: #3a3d47; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ----- Tooltips ----- */
QToolTip {{
    background-color: {BG_CARD_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_HOVER};
    border-radius: 7px;
    padding: 5px 9px;
}}

/* ----- Status bar ----- */
QStatusBar {{
    background: {BG_CHROME};
    border-top: 1px solid {BORDER_HAIRLINE};
    color: {TEXT_MID};
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    """Install the Obsidian stylesheet + palette + font globally."""
    family = load_fonts()

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_WINDOW))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_INPUT))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_CARD))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_HIGH))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_SELECTION))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_CARD_RAISED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyle("Fusion")  # Fusion respects palette/QSS most reliably
    # App-wide font so QPainter-driven delegates (recordings cards) and any
    # non-QSS text pick up Plus Jakarta Sans too.
    base_font = QFont(family, 10)
    app.setFont(base_font)
    app.setStyleSheet(_QSS)
