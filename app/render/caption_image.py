"""Render a caption to a transparent PNG — the locked style.

TikTok Sans (heavy), white fill + dark stroke, centered, blank-line stanza spacing, static.
Auto-fits the font size so the widest line stays inside the IG safe zone. Composited
losslessly over the video by the compositor.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from app.config import settings


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(settings.font_path, size)
    # Variable font: pick the heaviest sensible named instance for the TikTok look.
    try:
        names = font.get_variation_names()
        chosen = None
        for pref in (b"Black", b"ExtraBold", b"Bold", b"SemiBold", b"Medium"):
            for n in names:
                nb = n if isinstance(n, bytes) else str(n).encode()
                if pref.lower() in nb.lower():
                    chosen = n
                    break
            if chosen:
                break
        if chosen is not None:
            font.set_variation_by_name(chosen)
    except Exception:
        pass
    return font


def render_caption_png(
    text: str,
    out_path: str,
    width: int | None = None,
    height: int | None = None,
    max_font: int = 74,
    min_font: int = 34,
    stroke: int = 8,
    y_frac: float = 0.42,
    margin_frac: float = 0.86,
) -> str:
    width = width or settings.reel_width
    height = height or settings.reel_height
    lines = [ln for ln in text.split("\n")]

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    size = max_font
    while size > min_font:
        font = _load_font(size)
        widest = max((probe.textlength(ln, font=font) for ln in lines if ln.strip()), default=0.0)
        if widest <= width * margin_frac:
            break
        size -= 2

    font = _load_font(size)
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
        spacing=int(size * 0.35),
    )
    img.save(out_path)
    return out_path
