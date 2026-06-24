"""Render a caption to a transparent PNG — the locked TikTok-caption style.

TikTok Sans (heavy), white fill + a thin dark outline, centered, word-WRAPPED to fit the frame
so nothing overflows, at a CONSISTENT size (so the outline weight stays uniform reel to reel),
upper-third placement, static. Composited losslessly over the video by the compositor.
"""
from __future__ import annotations

import re

from PIL import Image, ImageDraw, ImageFont

from app.config import settings

# Weight axis order for TikTokSans-VariableFont: [Optical size, Width, Weight, Slant].
_AXES = lambda weight: [36, 100, weight, 0]  # noqa: E731

# The caption font has no emoji glyphs (they render as ▢ boxes). Map the load-bearing 🥷 to text
# so the ninja move still reads, and strip any other emoji so nothing renders as a box.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF️‍]"
)


def _sanitize(text: str) -> str:
    text = text.replace("🥷's", "ninjas").replace("🥷", "ninja")
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"[ ]{2,}", " ", text).strip()


def _load_font(size: int, weight: int = 800) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(settings.font_path, size)
    try:
        font.set_variation_by_axes(_AXES(weight))  # variable font defaults to 300 (Light)
    except Exception:
        try:
            font.set_variation_by_name(b"ExtraBold")
        except Exception:
            pass
    return font


def _wrap(paras: list[str], font, max_w: float, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap each paragraph to max_w; keep blank lines as stanza gaps (the engine's \\n\\n)."""
    out: list[str] = []
    for p in paras:
        if not p.strip():
            out.append("")
            continue
        cur = ""
        for word in p.split():
            test = f"{cur} {word}".strip()
            if not cur or draw.textlength(test, font=font) <= max_w:
                cur = test
            else:
                out.append(cur)
                cur = word
        if cur:
            out.append(cur)
    return out


def render_caption_png(
    text: str,
    out_path: str,
    width: int | None = None,
    height: int | None = None,
    font_size: int = 70,
    min_font: int = 52,
    weight: int = 800,
    stroke_frac: float = 0.067,
    y_frac: float = 0.30,
    margin_frac: float = 0.86,
    max_lines: int = 5,
) -> str:
    width = width or settings.reel_width
    height = height or settings.reel_height
    max_w = width * margin_frac
    paras = _sanitize(text).split("\n")
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    # Hold the size CONSISTENT (wrap to fit width); only shrink if a caption is so long it
    # would exceed max_lines after wrapping.
    size = font_size
    while size > min_font:
        lines = _wrap(paras, _load_font(size, weight), max_w, probe)
        if sum(1 for ln in lines if ln) <= max_lines:
            break
        size -= 3

    font = _load_font(size, weight)
    lines = _wrap(paras, font, max_w, probe)
    final = "\n".join(lines)
    stroke = max(2, round(size * stroke_frac))

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (width / 2, height * y_frac),
        final,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        align="center",
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 255),
        spacing=int(size * 0.30),
    )
    img.save(out_path)
    return out_path
