"""Global dark theme — applied process-wide in :func:`apply_dark_theme`.

A single QSS string tunes Qt's stock widgets toward a modern, low-contrast
dark look. Goals:

* Single accent color (cool blue) so it reads as one product
* Soft surfaces / panels with subtle borders rather than hard 1-pixel lines
* Generous spacing on form controls
* Rounded corners on interactive elements

Colour tokens grouped at the top so a future light theme is a swap, not a
rewrite.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

# --- palette ---------------------------------------------------------------
BG_WINDOW = "#15171c"       # outermost backgrounds
BG_PANEL = "#1d2027"        # group boxes, splitter children
BG_INPUT = "#262a33"        # inputs, combo box drop-downs
BG_HOVER = "#2e333e"
BG_PRESS = "#3a4150"
BORDER = "#34394a"
BORDER_STRONG = "#445063"
TEXT = "#e6e8ee"
TEXT_DIM = "#9aa1b1"
TEXT_DIM_2 = "#6e7588"
ACCENT = "#8b5cf6"          # violet — primary action / focus / selection
ACCENT_HOVER = "#a78bf9"
ACCENT_PRESS = "#7245d8"
DANGER = "#e05462"


_QSS = f"""
* {{
    color: {TEXT};
    font-family: "Segoe UI", "Inter", "SF Pro Display", sans-serif;
    font-size: 10pt;
}}

QMainWindow, QDialog, QWidget#centralwidget {{
    background-color: {BG_WINDOW};
}}

QWidget {{
    background-color: transparent;
    selection-background-color: {ACCENT};
    selection-color: white;
}}

QFrame[frameShape="4"],   /* HLine */
QFrame[frameShape="5"] {{  /* VLine */
    color: {BORDER};
}}

QSplitter::handle {{
    background: {BORDER};
    width: 1px;
    height: 1px;
}}

/* ----- Menus ----- */
QMenuBar {{
    background-color: {BG_WINDOW};
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
    border-radius: 4px;
}}
QMenuBar::item:selected {{ background: {BG_HOVER}; }}
QMenu {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    padding: 4px;
    border-radius: 6px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 4px;
}}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}

/* ----- Group boxes (Settings sections) ----- */
QGroupBox {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 18px;
    padding: 22px 14px 14px 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 8px;
    color: {TEXT_DIM};
    font-weight: 600;
    background: {BG_WINDOW};
    text-transform: uppercase;
    font-size: 9pt;
    letter-spacing: 1px;
}}

QTabWidget::pane {{
    border: none;
    background: transparent;
    margin-top: 4px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 8px 18px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:hover {{
    color: {TEXT};
    background: {BG_PANEL};
}}
QTabBar::tab:selected {{
    color: {TEXT};
    background: {BG_PANEL};
    border-color: {BORDER};
    border-bottom: 1px solid {BG_PANEL};
}}

/* ----- Buttons ----- */
QPushButton {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 22px;
}}
QPushButton:hover {{ background-color: {BG_HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background-color: {BG_PRESS}; }}
QPushButton:disabled {{ color: {TEXT_DIM_2}; background-color: {BG_WINDOW}; }}

QPushButton#primary {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: white;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background-color: {ACCENT_PRESS}; }}
QPushButton#primary:disabled {{ background-color: #3a3f4b; border-color: #3a3f4b; }}

QDialogButtonBox QPushButton[text="Save"] {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: white;
    font-weight: 600;
}}
QDialogButtonBox QPushButton[text="Save"]:hover {{ background-color: {ACCENT_HOVER}; }}

/* ----- Inputs ----- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}
/* Inline rename editor used by tree / list / table views — the global
   QLineEdit padding + border-radius bursts out of the row otherwise. */
QAbstractItemView QLineEdit {{
    padding: 0 2px;
    border-radius: 0;
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: white;
    padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 16px;
}}

/* ----- Check / radio ----- */
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_STRONG};
    background: {BG_INPUT};
    border-radius: 4px;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

/* ----- Sliders ----- */
QSlider::groove:horizontal {{
    height: 4px;
    background: {BG_INPUT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -6px 0;
    background: {ACCENT};
    border-radius: 7px;
    border: 2px solid {TEXT};
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}

/* ----- Progress bar (export progress) ----- */
QProgressBar {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    height: 18px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

/* ----- Tables (editor list — will be replaced by QListView, but style for now) ----- */
QHeaderView::section {{
    background-color: {BG_PANEL};
    color: {TEXT_DIM};
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}
QTableWidget, QListWidget, QListView, QTreeView {{
    background-color: {BG_WINDOW};
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {BORDER};
    outline: none;
}}
QTableWidget::item, QListWidget::item, QListView::item, QTreeView::item {{
    padding: 6px 6px;
    border: none;
}}
QTableWidget::item:selected, QListWidget::item:selected,
QListView::item:selected, QTreeView::item:selected {{
    background: {ACCENT};
    color: white;
}}
QTableWidget::item:hover, QListWidget::item:hover,
QListView::item:hover, QTreeView::item:hover {{
    background: {BG_HOVER};
}}

/* ----- Scroll bars ----- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM_2}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_DIM_2}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ----- Tooltips ----- */
QToolTip {{
    background-color: {BG_PANEL};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    padding: 4px 8px;
}}

/* ----- Status bar (used in editor) ----- */
QStatusBar {{
    background: {BG_WINDOW};
    border-top: 1px solid {BORDER};
    color: {TEXT_DIM};
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    """Install the dark stylesheet + palette globally on the QApplication."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_WINDOW))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_WINDOW))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyle("Fusion")  # Fusion respects palette/QSS most reliably across Win/Linux
    app.setStyleSheet(_QSS)
