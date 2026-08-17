"""Regenerate every Momento brand raster from the Obsidian squircle
(violet→magenta gradient, white "M", yellow spark).

Renders the mark crisply via the same Qt painter the in-app brand uses
(momento.ui.theme.logo_pixmap), then writes:

  resources/icons/momento.ico     — window/taskbar icon, toast mark, exe embed
  resources/icons/momento.png     — 512px canonical raster
  resources/icons/momento-256.png — landing-page hero/favicon source
  resources/icons/momento-120.png — Cloud Console branding (square, <1 MB)
  docs/momento.png                — GitHub Pages copy (512)
  docs/momento-256.png            — GitHub Pages favicon/hero/og:image (256)
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtWidgets import QApplication
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "resources" / "icons"
DOCS = ROOT / "docs"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _render(size: int) -> Image.Image:
    from momento.ui import theme

    pm = theme.logo_pixmap(size, device_ratio=1.0)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    return Image.open(io.BytesIO(bytes(ba))).convert("RGBA")


def main() -> int:
    app = QApplication([])  # noqa: F841 — needed for QPixmap

    png512 = _render(512)
    png256 = _render(256)
    png120 = _render(120)
    base = _render(256)  # .ico source (Pillow downsamples to each size)

    outputs = {
        ICONS / "momento.png": png512,
        ICONS / "momento-256.png": png256,
        ICONS / "momento-120.png": png120,
        DOCS / "momento.png": png512,
        DOCS / "momento-256.png": png256,
    }
    for path, img in outputs.items():
        img.save(path)

    base.save(ICONS / "momento.ico", format="ICO", sizes=[(s, s) for s in ICO_SIZES])

    # Sanity: the Phase-13 footgun was a baked-white (RGB) source. Confirm the
    # corners are genuinely transparent so nothing carries a white box.
    corner = base.getpixel((0, 0))
    assert corner[3] == 0, f"corner not transparent: {corner}"
    print("Regenerated brand rasters:")
    for path in (ICONS / "momento.ico", *outputs.keys()):
        print(f"  {path.relative_to(ROOT)}")
    print(f"(corner alpha={corner[3]} OK)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
