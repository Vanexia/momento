"""Inline SVG glyph set for the Obsidian redesign.

The design's UI icons are 1.6–1.8px-stroke inline SVGs. Rather than ship a raster
icon set, we keep the exact path data here and render each glyph through QtSvg,
recoloured on demand, at any size/DPI. Results are cached.

    pm = icons.pixmap("mic", 14, theme.ACCENT_VIOLET)
    lbl = icons.label("gear", 13, theme.TEXT_MID)      # a QLabel wrapping the pixmap
    btn_icon = icons.qicon("download", 15, theme.ACCENT_VIOLET)
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QLabel

# Each entry: (viewBox, inner-svg-with-{c}-colour-placeholder). The {c} token is
# substituted for the requested colour everywhere it appears (stroke and/or fill).
_GLYPHS: dict[str, tuple[str, str]] = {
    "gear": (
        "0 0 24 24",
        '<circle cx="12" cy="12" r="3.2" stroke="{c}" stroke-width="1.7" fill="none"/>'
        '<path d="M12 2.5v2.4M12 19.1v2.4M21.5 12h-2.4M4.9 12H2.5M18.7 5.3l-1.7 1.7'
        'M7 17l-1.7 1.7M18.7 18.7L17 17M7 7L5.3 5.3" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round" fill="none"/>',
    ),
    "mic": (
        "0 0 24 24",
        '<rect x="9" y="3" width="6" height="11" rx="3" stroke="{c}" stroke-width="1.7" fill="none"/>'
        '<path d="M6 11a6 6 0 0 0 12 0M12 17v3" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round" fill="none"/>',
    ),
    "speaker": (
        "0 0 24 24",
        '<path d="M4 9v6h4l5 4V5L8 9H4z" stroke="{c}" stroke-width="1.6" '
        'stroke-linejoin="round" fill="none"/>'
        '<path d="M16 9c1 1.5 1 4.5 0 6M18.5 7c2 2.5 2 7.5 0 10" stroke="{c}" '
        'stroke-width="1.6" fill="none"/>',
    ),
    "search": (
        "0 0 24 24",
        '<circle cx="11" cy="11" r="7" stroke="{c}" stroke-width="1.8" fill="none"/>'
        '<path d="M20 20l-3.5-3.5" stroke="{c}" stroke-width="1.8" '
        'stroke-linecap="round" fill="none"/>',
    ),
    "chevron-down": (
        "0 0 12 12",
        '<path d="M2 4l4 4 4-4" stroke="{c}" stroke-width="1.6" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>',
    ),
    "chevron-left": (
        "0 0 24 24",
        '<path d="M15 6l-6 6 6 6" stroke="{c}" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" fill="none"/>',
    ),
    "chevron-right": (
        "0 0 24 24",
        '<path d="M9 6l6 6-6 6" stroke="{c}" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" fill="none"/>',
    ),
    "play": (
        "0 0 24 24",
        '<path d="M8 5v14l11-7z" fill="{c}"/>',
    ),
    "pause": (
        "0 0 24 24",
        '<rect x="6" y="5" width="4" height="14" rx="1" fill="{c}"/>'
        '<rect x="14" y="5" width="4" height="14" rx="1" fill="{c}"/>',
    ),
    "prev": (
        "0 0 24 24",
        '<path d="M11 7l-6 5 6 5V7zM19 7l-6 5 6 5V7z" fill="{c}"/>',
    ),
    "next": (
        "0 0 24 24",
        '<path d="M13 7l6 5-6 5V7zM5 7l6 5-6 5V7z" fill="{c}"/>',
    ),
    "volume": (
        "0 0 24 24",
        '<path d="M4 9v6h4l5 4V5L8 9H4z" fill="{c}"/>'
        '<path d="M16 9c1 1.5 1 4.5 0 6" stroke="{c}" stroke-width="1.6" fill="none"/>',
    ),
    "mute": (
        "0 0 24 24",
        '<path d="M4 9v6h4l5 4V5L8 9H4z" fill="{c}"/>'
        '<path d="M16.5 9.5l5 5M21.5 9.5l-5 5" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round" fill="none"/>',
    ),
    "fullscreen": (
        "0 0 24 24",
        '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    ),
    "download": (
        "0 0 24 24",
        '<path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" stroke="{c}" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    ),
    "folder": (
        "0 0 24 24",
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" '
        'stroke="{c}" stroke-width="1.7" stroke-linejoin="round" fill="none"/>',
    ),
    "refresh": (
        "0 0 24 24",
        '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16'
        'M3 21v-5h5" stroke="{c}" stroke-width="1.7" stroke-linecap="round" '
        'stroke-linejoin="round" fill="none"/>',
    ),
    "youtube": (
        "0 0 24 24",
        '<path d="M21.6 7.2a2.7 2.7 0 0 0-1.9-1.9C18 4.8 12 4.8 12 4.8s-6 0-7.7.5'
        'A2.7 2.7 0 0 0 2.4 7.2C1.9 8.9 1.9 12 1.9 12s0 3.1.5 4.8a2.7 2.7 0 0 0 1.9 1.9'
        'c1.7.5 7.7.5 7.7.5s6 0 7.7-.5a2.7 2.7 0 0 0 1.9-1.9c.5-1.7.5-4.8.5-4.8s0-3.1-.5-4.8z'
        'M10 15V9l5.2 3L10 15z" fill="{c}"/>',
    ),
    "star": (
        "0 0 24 24",
        '<path d="M12 3l2.4 6.9H22l-5.8 4.3 2.2 7L12 16.9 5.6 21.2l2.2-7L2 9.9h7.6z" '
        'fill="{c}"/>',
    ),
    # ----- settings nav rail -----
    "capture": (
        "0 0 24 24",
        '<rect x="3" y="6" width="13" height="12" rx="2" stroke="{c}" stroke-width="1.7" fill="none"/>'
        '<path d="M16 10l5-3v10l-5-3" stroke="{c}" stroke-width="1.7" '
        'stroke-linejoin="round" fill="none"/>',
    ),
    "output": (
        "0 0 24 24",
        '<path d="M4 4h16v12H4zM2 20h20M9 8h6" stroke="{c}" stroke-width="1.7" '
        'stroke-linejoin="round" fill="none"/>',
    ),
    "games": (
        "0 0 24 24",
        '<rect x="2" y="6" width="20" height="12" rx="3" stroke="{c}" stroke-width="1.7" fill="none"/>'
        '<path d="M7 12h3M8.5 10.5v3M15 11h.01M18 13h.01" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round" fill="none"/>',
    ),
    "bell": (
        "0 0 24 24",
        '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9zM13.7 21a2 2 0 0 1-3.4 0" '
        'stroke="{c}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    ),
    "power": (
        "0 0 24 24",
        '<path d="M12 2v6M5.6 8.6A8 8 0 1 0 18.4 8.6" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round" fill="none"/>',
    ),
    "youtube-box": (
        "0 0 24 24",
        '<rect x="2" y="5" width="20" height="14" rx="4" stroke="{c}" stroke-width="1.7" fill="none"/>'
        '<path d="M10 9l5 3-5 3z" fill="{c}"/>',
    ),
}


_cache: dict[tuple, QPixmap] = {}


def pixmap(name: str, size: int, color: str, dpr: float | None = None) -> QPixmap:
    """Render glyph ``name`` at ``size`` device-independent px in ``color``."""
    if dpr is None:
        app = QApplication.instance()
        dpr = float(app.devicePixelRatio()) if app is not None else 1.0
    key = (name, size, color, round(dpr, 3))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    view_box, body = _GLYPHS.get(name, _GLYPHS["chevron-down"])
    # Pad the viewBox so strokes that sit near the glyph's nominal bounds (gear
    # ticks, star tips, speaker, fullscreen brackets) aren't clipped by the
    # render edge. The renderer maps the padded box to the full pixmap, so the
    # content lands centred with a little breathing room.
    minx, miny, vw, vh = (float(v) for v in view_box.split())
    m = max(vw, vh) * 0.09
    padded = f"{minx - m} {miny - m} {vw + 2 * m} {vh + 2 * m}"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{padded}" '
        f'width="{size}" height="{size}">{body.format(c=color)}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    try:
        # Render into the LOGICAL rect (device px / dpr). The painter on a
        # high-DPI pixmap already carries the dpr scale, so passing an explicit
        # logical target keeps the glyph from being drawn dpr× too large and
        # overflowing the bottom-right of the pixmap (the clipping bug at >1×).
        logical = px / dpr
        renderer.render(p, QRectF(0.0, 0.0, logical, logical))
    finally:
        p.end()
    _cache[key] = pm
    return pm


def label(name: str, size: int, color: str) -> QLabel:
    """A non-interactive QLabel holding the glyph pixmap."""
    lbl = QLabel()
    lbl.setPixmap(pixmap(name, size, color))
    lbl.setFixedSize(size, size)
    lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return lbl


def qicon(name: str, size: int, color: str) -> QIcon:
    return QIcon(pixmap(name, size, color))
