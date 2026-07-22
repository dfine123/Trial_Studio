"""Render a caption to a transparent PNG — the locked TikTok-caption style, WITH emoji.

TikTok Sans (heavy) white fill + thin dark outline, word-wrapped, centered, upper-third. Emoji
(🥷 🙏 😭 💀 …) render in COLOR from the local Noto Color Emoji font via Pilmoji — fully offline,
no CDN. The caption engine is free to use emoji as the references do; the renderer handles them,
they are never stripped or a constraint on generation.
"""
from __future__ import annotations

import os
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from pilmoji.source import AppleEmojiSource, BaseSource

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


class _AppleThenNotoSource(BaseSource):
    """Apple / iOS emoji (via the emoji CDN) — the look the creator wants — with the local Noto
    font as an OFFLINE fallback so an emoji never renders as a box even if the CDN is unreachable."""

    def __init__(self):
        self._apple = AppleEmojiSource()
        self._noto = _NotoEmojiSource()

    def get_emoji(self, emoji: str):
        try:
            r = self._apple.get_emoji(emoji)
            if r:
                return r
        except Exception:  # noqa: BLE001 — CDN hiccup → fall back to local Noto
            pass
        return self._noto.get_emoji(emoji)

    def get_discord_emoji(self, id: int):  # noqa: A002
        return None


# ── FONT STYLES (2026-07-21): 5 tasteful options beyond the base TikTok look. Each style is a
# complete treatment — face, weight, stroke vs soft shadow, sizing — not just a font swap.
# "base" stays the locked default; the operator picks per generation (UI "Font").
_FONT_STYLES: dict[str, dict] = {
    "base":       {"path": None, "var": None, "size_mult": 1.0, "stroke": True, "stroke_frac": None,
                   "shadow": False, "spacing": 0.26, "tracking": 0, "case": None},
    # CINEMATIC treatments (2026-07-21 rework): smaller, tracked-out, lighter — title-card energy
    # elegant high-contrast serif — the motivational/cinematic centerpiece
    "elegant":    {"path": "fonts/PlayfairDisplay.ttf", "var": "Medium", "size_mult": 0.80,
                   "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.52,
                   "tracking": 2, "case": None},
    # minimal cinematic caps — thin grotesque, wide tracking (A24 title-card look)
    "clean":      {"path": "fonts/Montserrat.ttf", "var": "Light", "size_mult": 0.72,
                   "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.55,
                   "tracking": 3, "case": "upper"},
    # typewriter — notes-app/confession energy
    "typewriter": {"path": "fonts/CourierPrime-Bold.ttf", "var": None, "size_mult": 0.90,
                   "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.34,
                   "tracking": 0, "case": None},
    # handwritten pen — personal, bro-line energy
    "handwritten": {"path": "fonts/Caveat.ttf", "var": "Bold", "size_mult": 1.35,
                    "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.18,
                    "tracking": 0, "case": None},
    # condensed poster caps — only works WITH a stroke (operator call): thin outline + shadow
    "poster":     {"path": "fonts/BebasNeue-Regular.ttf", "var": None, "size_mult": 1.12,
                   "stroke": True, "stroke_frac": 0.040, "shadow": True, "spacing": 0.26,
                   "tracking": 1, "case": None},
}

def _style_line(line: str, spec: dict) -> str:
    """Case transform only — tracking is applied at measure/draw time (per-char advances),
    never by injecting whitespace (hair spaces are whitespace: they shatter word wrap)."""
    if spec.get("case") == "upper":
        line = line.upper()
    return line


def _track_px(size: int, spec: dict) -> int:
    """Letter-tracking in pixels for this style at this size (0 = normal)."""
    n = spec.get("tracking") or 0
    return int(size * 0.055 * n) if n else 0


def _line_w(line: str, font, draw, tpx: int) -> float:
    if not tpx or any(ord(ch) > 0x2500 for ch in line):
        return draw.textlength(line, font=font)
    return sum(draw.textlength(ch, font=font) for ch in line) + tpx * max(0, len(line) - 1)



def _load_font(size: int, weight: int = 800, style: str = "base") -> ImageFont.FreeTypeFont:
    spec = _FONT_STYLES.get(style) or _FONT_STYLES["base"]
    if spec["path"]:
        font = ImageFont.truetype(spec["path"], size)
        if spec["var"]:
            try:
                font.set_variation_by_name(spec["var"])
            except Exception:  # noqa: BLE001 — static font or missing named instance
                pass
        return font
    font = ImageFont.truetype(settings.font_path, size)
    try:
        font.set_variation_by_axes(_AXES(weight))
    except Exception:
        try:
            font.set_variation_by_name(b"ExtraBold")
        except Exception:
            pass
    return font


def _wrap(paras: list[str], font, max_w: float, draw: ImageDraw.ImageDraw, tpx: int = 0) -> list[str]:
    """Word-wrap each paragraph to max_w; keep blank lines as stanza gaps (the engine's \\n\\n)."""
    out: list[str] = []
    for p in paras:
        if not p.strip():
            out.append("")
            continue
        cur = ""
        for word in p.split():
            test = f"{cur} {word}".strip()
            if not cur or _line_w(test, font, draw, tpx) <= max_w:
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
    font_size: int = 56,
    min_font: int = 44,
    weight: int = 800,
    stroke_frac: float = 0.067,
    y_frac: float = 0.30,
    margin_frac: float = 0.86,
    max_lines: int = 4,
    font_style: str = "base",
) -> str:
    spec = _FONT_STYLES.get(font_style) or _FONT_STYLES["base"]
    width = width or settings.reel_width
    height = height or settings.reel_height
    max_w = width * margin_frac
    paras = [_style_line(p, spec) if p.strip() else p for p in text.split("\n")]
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    font_size = int(font_size * spec["size_mult"])
    min_font = int(min_font * spec["size_mult"])
    size = font_size
    while size > min_font:
        lines = _wrap(paras, _load_font(size, weight, font_style), max_w, probe, _track_px(size, spec))
        if sum(1 for ln in lines if ln) <= max_lines:
            break
        size -= 3

    font = _load_font(size, weight, font_style)
    tpx = _track_px(size, spec)
    lines = _wrap(paras, font, max_w, probe, tpx)
    stroke = max(2, round(size * (spec.get("stroke_frac") or stroke_frac))) if spec["stroke"] else 0
    spacing = int(size * spec["spacing"])

    # Lay the lines out MANUALLY and render each as a SINGLE Pilmoji call — Pilmoji's own multiline
    # rendering botches the stroke on every line past the first, so we stack the lines ourselves
    # (each single-line call strokes correctly). Block stays centered on y_frac.
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    step = line_h + spacing
    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
    top = height * y_frac - total_h / 2.0

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    with Pilmoji(img, source=_AppleThenNotoSource) as pilmoji:
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            cy = top + i * step + line_h / 2.0
            off = max(2, size // 22)
            track = tpx and not any(ord(ch) > 0x2500 for ch in line)
            if track:
                lw = _line_w(line, font, probe, tpx)
                for pass_shadow in ([True] if spec["shadow"] else []) + [False]:
                    x = width / 2.0 - lw / 2.0
                    for ch in line:
                        pos = (int(x) + (off if pass_shadow else 0), int(cy) + (off if pass_shadow else 0))
                        pilmoji.text(pos, ch, font=font,
                                     fill=(0, 0, 0, 150) if pass_shadow else (255, 255, 255, 255),
                                     anchor="lm",
                                     stroke_width=0 if pass_shadow else stroke,
                                     stroke_fill=(0, 0, 0, 255))
                        x += probe.textlength(ch, font=font) + tpx
                continue
            if spec["shadow"]:
                # soft drop shadow instead of the hard meme outline — the tasteful styles
                pilmoji.text((width // 2 + off, int(cy) + off), line, font=font,
                             fill=(0, 0, 0, 150), anchor="mm", emoji_scale_factor=1.15)
            pilmoji.text(
                (width // 2, int(cy)),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                anchor="mm",
                stroke_width=stroke,
                stroke_fill=(0, 0, 0, 255),
                emoji_scale_factor=1.15,
            )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)  # tmp/ may not exist on a fresh host
    img.save(out_path)
    return out_path
