"""Render a caption to a transparent PNG — the locked TikTok-caption style, WITH emoji.

TikTok Sans (heavy) white fill + thin dark outline, word-wrapped, centered, upper-third. Emoji
(🥷 🙏 😭 💀 …) render in COLOR from the local Noto Color Emoji font via Pilmoji — fully offline,
no CDN. The caption engine is free to use emoji as the references do; the renderer handles them,
they are never stripped or a constraint on generation.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from pilmoji.source import BaseSource

from app.config import settings

# Weight axis order for TikTokSans-VariableFont: [Optical size, Width, Weight, Slant].
_AXES = lambda weight: [36, 100, weight, 0]  # noqa: E731
_NOTO_PATH = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"


class _NotoEmojiSource(BaseSource):
    """Emoji glyphs from the local Noto Color Emoji font (offline). Noto is a bitmap font with a
    single 109px strike, so it must be opened at exactly that size; Pilmoji resizes from there."""

    def __init__(self):
        try:
            self._font = ImageFont.truetype(_NOTO_PATH, 109)
        except Exception:  # noqa: BLE001 — degrade gracefully (emoji just won't draw, no crash)
            self._font = None

    def get_emoji(self, emoji: str):
        if self._font is None:
            return None
        img = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((6, 6), emoji, font=self._font, embedded_color=True)
        bbox = img.getbbox()
        if not bbox:
            return None
        bio = BytesIO()
        img.crop(bbox).save(bio, "PNG")
        bio.seek(0)
        return bio

    def get_discord_emoji(self, id: int):  # noqa: A002 — required by the BaseSource interface
        return None


def _load_font(size: int, weight: int = 800) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(settings.font_path, size)
    try:
        font.set_variation_by_axes(_AXES(weight))
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
    paras = text.split("\n")
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    size = font_size
    while size > min_font:
        lines = _wrap(paras, _load_font(size, weight), max_w, probe)
        if sum(1 for ln in lines if ln) <= max_lines:
            break
        size -= 3

    font = _load_font(size, weight)
    final = "\n".join(_wrap(paras, font, max_w, probe))
    stroke = max(2, round(size * stroke_frac))

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    with Pilmoji(img, source=_NotoEmojiSource) as pilmoji:
        pilmoji.text(
            (width // 2, int(height * y_frac)),
            final,
            font=font,
            fill=(255, 255, 255, 255),
            anchor="mm",
            align="center",
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, 255),
            spacing=int(size * 0.30),
            emoji_scale_factor=1.15,
        )
    img.save(out_path)
    return out_path
