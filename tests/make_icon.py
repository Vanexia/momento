"""Generate resources/icons/momento.ico (multi-resolution).

Design: rounded-square in Momento's deep blue with a bold antialiased "M"
glyph rendered via a TTF font (Segoe UI / Arial fallback). No record-state
indicator on the icon itself — that would mislead users into thinking the
app is recording all the time. Recording state is communicated by the tray
icon overlay and the toast's own state dot.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "resources" / "icons" / "momento.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Palette — matches momento.ui.theme
BG_TOP = (38, 46, 70, 255)       # gradient top (soft navy)
BG_BOTTOM = (24, 28, 42, 255)    # gradient bottom (deeper navy)
GLYPH = (236, 240, 252, 255)     # near-white M
GLYPH_ACCENT = (160, 190, 255, 200)  # faint inner highlight


def _find_bold_font(size: int) -> ImageFont.ImageFont:
    """Pick a clean bold sans-serif at the requested px size, with fallbacks."""
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf",   # Segoe UI Bold — Windows native
        "C:/Windows/Fonts/seguibl.ttf",    # Segoe UI Black (heavier)
        "C:/Windows/Fonts/arialbd.ttf",    # Arial Bold
        "C:/Windows/Fonts/calibrib.ttf",   # Calibri Bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, IOError):
            continue
    # PIL default — bitmap font, not great but at least it draws.
    return ImageFont.load_default()


def _draw_gradient_rounded(size: int, radius: int, pad: int) -> Image.Image:
    """Vertical gradient inside a rounded square — better than a flat fill."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # Build the gradient as a column then crop to the rounded mask.
    grad = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        r = round(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = round(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = round(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        grad.putpixel((0, y), (r, g, b, 255))
    grad = grad.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle((pad, pad, size - pad, size - pad), radius=radius, fill=255)
    layer.paste(grad, (0, 0), mask)
    return layer


def _render_glyph_tight(target_height: int, color: tuple) -> Image.Image:
    """Render an "M" and crop it to its tight ink bbox.

    Fonts have ascender/descender padding so ``font_size`` ≠ glyph height.
    Render at a generous size, then crop to the actual painted pixels — the
    caller can then scale this to whatever it really wants the M to look like.
    """
    over_size = max(target_height * 3, 256)
    img = Image.new("RGBA", (over_size, over_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _find_bold_font(int(over_size * 0.9))
    draw.text((over_size // 2, over_size // 2), "M",
              font=font, fill=color, anchor="mm")
    # Crop to the actually-painted bounding box (uses the alpha channel).
    bbox = img.getbbox()
    if bbox is None:
        return img
    return img.crop(bbox)


def _draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pad = max(1, size // 20)
    radius = max(3, size // 5)

    # Background
    img.alpha_composite(_draw_gradient_rounded(size, radius, pad))

    # Glyph: M scaled to fill ~80% of the icon's height. We render at large
    # size, crop to the M's tight bbox, then resize — guarantees a consistent
    # visual size regardless of the chosen font's ascender/descender metrics.
    target_h = int(size * 0.78)
    glyph = _render_glyph_tight(target_h, GLYPH)
    # Scale to target height while keeping aspect ratio.
    scale = target_h / glyph.height
    new_w = max(1, int(round(glyph.width * scale)))
    new_h = max(1, int(round(glyph.height * scale)))
    glyph = glyph.resize((new_w, new_h), Image.Resampling.LANCZOS)
    gx = (size - new_w) // 2
    gy = (size - new_h) // 2
    img.alpha_composite(glyph, dest=(gx, gy))

    # Rim-light: render an accent-colour M, scale identically, paste a few
    # pixels above the main M, then mask everything below the top third so the
    # highlight reads as a top edge glow rather than a full second M.
    if size >= 48:
        highlight = _render_glyph_tight(target_h, GLYPH_ACCENT)
        highlight = highlight.resize((new_w, new_h), Image.Resampling.LANCZOS)
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        layer.alpha_composite(highlight, dest=(gx, gy - max(1, size // 90)))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rectangle((0, 0, size, size // 3), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(radius=max(1, size // 40)))
        layer_alpha = layer.split()[3]
        layer.putalpha(Image.eval(
            Image.composite(layer_alpha, Image.new("L", (size, size), 0), mask),
            lambda v: min(v, 140),
        ))
        img.alpha_composite(layer)

    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    images = [_draw(s) for s in SIZES]
    images[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Wrote {OUT} (sizes: {SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
