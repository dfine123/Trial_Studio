"""Render a caption to a transparent PNG — the locked TikTok-caption style.

TikTok Sans (heavy), white fill + a thin dark outline, centered, blank-line stanza spacing,
upper-third placement, static. Auto-fits the font size so the widest line stays inside the safe
zone. Composited losslessly over the video by the compositor.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from app.config import settings

# Weight axis order for TikTokSans-VariableFont: [Optical size, Width, Weight, Slant].
_AXES = lambda weight: [36, 100, weight, 0]  # noqa: E731


def _load_font(size: int, weight: int = 800) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(settings.font_path, size)
    # Set the Weight axis directly (reliable) — the variable font defaults to 300 (Light).
    try:
        font.set_variation_by_axes(_AXES(weight))
    except Exception:
        try:
            font.set_variation_by_name(b"ExtraBold")
        except Exception:
            pass
    return font


def render_caption_png(
    text: str,
    out_path: str,
    width: int | None = None,
    height: int | None = None,
    max_font: int = 86,
    min_font: int = 42,
    weight: int = 800,
    stroke_frac: float = 0.065,
    y_frac: float = 0.31,
    margin_frac: float = 0.90,
) -> str:
    width = width or settings.reel_width
    height = height or settings.reel_height
    lines = text.split("\n")

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    size = max_font
    while size > min_font:
        font = _load_font(size, weight)
        widest = max((probe.textlength(ln, font=font) for ln in lines if ln.strip()), default=0.0)
        if widest <= width * margin_frac:
            break
        size -= 2

    font = _load_font(size, weight)
    stroke = max(2, round(size * stroke_frac))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (width / 2, height * y_frac),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        align="center",
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 255),
        spacing=int(size * 0.28),
    )
    img.save(out_path)
    return out_path
