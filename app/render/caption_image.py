"""Render a caption to a transparent PNG — the locked TikTok-caption style, WITH emoji.

TikTok Sans (heavy) white fill + thin dark outline, word-wrapped, centered, upper-third. Emoji
(🥷 🙏 😭 💀 …) render in COLOR from the local Noto Color Emoji font via Pilmoji — fully offline,
no CDN. The caption engine is free to use emoji as the references do; the renderer handles them,
they are never stripped or a constraint on generation.
"""
from __future__ import annotations

import os
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
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
    # the classic lighter TikTok caption look — same face, medium weight, thinner outline
    "slim":       {"path": None, "var": None, "weight": 550, "size_mult": 0.98, "stroke": True,
                   "stroke_frac": 0.052, "shadow": False, "spacing": 0.28, "tracking": 0, "case": None},
    # CINEMATIC treatments (2026-07-21 rework): smaller, tracked-out, lighter — title-card energy
    # elegant high-contrast serif — the motivational/cinematic centerpiece
    "elegant":    {"path": "fonts/PlayfairDisplay.ttf", "var": "SemiBold", "size_mult": 0.82,
                   "stroke": True, "stroke_frac": 0.038, "shadow": True, "spacing": 0.52,
                   "tracking": 2, "case": None},
    # minimal cinematic caps — thin grotesque, wide tracking (A24 title-card look)
    "clean":      {"path": "fonts/Montserrat.ttf", "var": "Regular", "size_mult": 0.74,
                   "stroke": True, "stroke_frac": 0.038, "shadow": True, "spacing": 0.55,
                   "tracking": 3, "case": "upper"},
    # typewriter — notes-app/confession energy
    "typewriter": {"path": "fonts/CourierPrime-Bold.ttf", "var": None, "size_mult": 0.90,
                   "stroke": True, "stroke_frac": 0.050, "shadow": True, "spacing": 0.34,
                   "tracking": 0, "case": None},
    # handwritten pen — personal, bro-line energy
    "handwritten": {"path": "fonts/Caveat.ttf", "var": "Bold", "size_mult": 1.35,
                    "stroke": True, "stroke_frac": 0.042, "shadow": True, "spacing": 0.18,
                    "tracking": 0, "case": None},
    # POLISHED display treatments (2026-07-22): a pronounced, "edited" look — heavy display
    # serif with a real outline, and a navy face wearing an ANIMATED gold gradient stroke.
    "punch":      {"path": "fonts/RobotoSlab.ttf", "var": "ExtraBold", "size_mult": 0.88,
                   "stroke": True, "stroke_frac": 0.075, "shadow": True, "spacing": 0.46,
                   "tracking": 0, "case": None, "sphere": 0.20,
                   "fill": (255, 208, 26), "stroke_color": (10, 9, 8)},
    "royal":      {"path": "fonts/Montserrat.ttf", "var": "Bold", "size_mult": 0.84,
                   "stroke": True, "stroke_frac": 0.040, "shadow": True, "spacing": 0.40,
                   "tracking": 1, "case": None, "adaptive_fill": True,
                   "gradient_stroke": "gold", "frames": 40, "fps": 8},
    # ── LOCKUP SET (2026-07-22): the operator pointed at Burberry — where the elevation comes
    # from FRAMING as much as the face: bold geometric caps, deliberate tracking, restrained
    # scale, air around the block. Every previous round was lowercase at default spacing, which
    # is why good faces still read like captions. Two treatments here: TIGHT caps (the Saville
    # lockup) and WIDE-tracked caps (the fashion-house lockup).
    "jost":       {"path": "fonts/Jost.ttf", "var": "Bold", "size_mult": 0.62, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.62, "tracking": -1,
                   "case": "upper"},
    "satoshi":    {"path": "fonts/Satoshi.ttf", "var": None, "size_mult": 0.60, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.62, "tracking": -1,
                   "case": "upper"},
    "switzer":    {"path": "fonts/Switzer.ttf", "var": None, "size_mult": 0.60, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.62, "tracking": -1,
                   "case": "upper"},
    "clash":      {"path": "fonts/ClashDisplay.ttf", "var": None, "size_mult": 0.62, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.62, "tracking": -1,
                   "case": "upper"},
    "montserrat_lock": {"path": "fonts/Montserrat.ttf", "var": "ExtraBold", "size_mult": 0.58,
                        "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.64,
                        "tracking": -1, "case": "upper"},
    "supreme":    {"path": "fonts/Supreme.ttf", "var": None, "size_mult": 0.58, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.66, "tracking": 2,
                   "case": "upper"},
    "archivo_lock": {"path": "fonts/ArchivoExp.ttf", "axes": [800, 100], "var": None,
                     "size_mult": 0.58, "stroke": False, "stroke_frac": None, "shadow": True,
                     "spacing": 0.66, "tracking": 2, "case": "upper"},
    "host":       {"path": "fonts/HostGrotesk.ttf", "var": "ExtraBold", "size_mult": 0.58,
                   "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.66,
                   "tracking": 2, "case": "upper"},
    "schibsted":  {"path": "fonts/SchibstedGrotesk.ttf", "var": "Black", "size_mult": 0.58,
                   "stroke": False, "stroke_frac": None, "shadow": True, "spacing": 0.66,
                   "tracking": 2, "case": "upper"},
    "boldonse":   {"path": "fonts/Boldonse.ttf", "var": None, "size_mult": 0.54, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.70, "tracking": 1,
                   "case": "upper"},
    "cabinet":    {"path": "fonts/CabinetGrotesk.ttf", "var": None, "size_mult": 0.60, "stroke": False,
                   "stroke_frac": None, "shadow": True, "spacing": 0.62, "tracking": -1,
                   "case": "upper"},
    "expanded":   {"path": "fonts/ArchivoExp.ttf", "axes": [700, 125], "var": None,
                   "size_mult": 0.56, "stroke": False, "stroke_frac": None, "shadow": True,
                   "spacing": 0.66, "tracking": 1, "case": "upper"},
    # condensed poster caps — only works WITH a stroke (operator call): thin outline + shadow
    "poster":     {"path": "fonts/BebasNeue-Regular.ttf", "var": None, "size_mult": 1.12,
                   "stroke": True, "stroke_frac": 0.040, "shadow": True, "spacing": 0.26,
                   "tracking": 1, "case": None},
}

_GOLD_STOPS = ((158, 108, 24), (214, 164, 48), (252, 214, 92), (255, 250, 228),
               (252, 214, 92), (214, 164, 48), (158, 108, 24))


def _gold_at(t: float) -> tuple[int, int, int]:
    """Colour at position t (0-1) of the repeating gold ramp — dark bronze → gold → highlight."""
    p = (t % 1.0) * (len(_GOLD_STOPS) - 1)
    i = int(p)
    f = p - i
    a, b = _GOLD_STOPS[i], _GOLD_STOPS[min(i + 1, len(_GOLD_STOPS) - 1)]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def _gold_layer(width: int, height: int, phase: float) -> Image.Image:
    """A diagonal gold sweep. Computed small and upscaled — a per-pixel pass at reel size would
    cost seconds per frame; bilinear upscaling of a smooth ramp is visually identical."""
    sw, sh = 96, 160
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    for y in range(sh):
        for x in range(sw):
            px[x, y] = _gold_at((x / sw) * 0.85 + (y / sh) * 0.45 + phase)
    return small.resize((width, height), Image.BILINEAR)


def _sphere_warp(img: Image.Image, strength: float = 0.30) -> Image.Image:
    """Bulge the text block like it's printed on a sphere — the CapCut-style warp on the
    operator's reference: the middle of the block swells, the ends fall away and tilt. Sampling
    is done on a 2x copy and downscaled so the outline keeps clean edges."""
    import numpy as np
    a = np.asarray(img.convert("RGBA"))
    ys, xs = np.nonzero(a[..., 3])
    if not len(xs):
        return img
    big = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    a = np.asarray(big.convert("RGBA"))
    h, w = a.shape[:2]
    cx, cy = (xs.min() + xs.max()), (ys.min() + ys.max())          # *2 centre (bbox mid x2)
    rx = max(1.0, (xs.max() - xs.min()))                            # *2 half-extent
    ry = max(1.0, (ys.max() - ys.min()))
    yy, xx = np.mgrid[0:h, 0:w]
    nx, ny = (xx - cx) / rx, (yy - cy) / ry
    r = np.sqrt(nx * nx + ny * ny)
    rc = np.clip(r, 1e-6, None)
    f = np.where(r < 1.0, r ** (1.0 + strength), r)                 # inside the block: magnify
    sx = np.clip((cx + nx * (f / rc) * rx).astype(np.int32), 0, w - 1)
    sy = np.clip((cy + ny * (f / rc) * ry).astype(np.int32), 0, h - 1)
    out = Image.fromarray(a[sy, sx])
    return out.resize((img.width, img.height), Image.LANCZOS)


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
    weight = spec.get("weight") or weight
    if spec["path"]:
        font = ImageFont.truetype(spec["path"], size)
        if spec.get("axes"):        # explicit axis values (e.g. weight + width)
            try:
                font.set_variation_by_axes(list(spec["axes"]))
            except Exception:  # noqa: BLE001
                pass
        elif spec["var"]:
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
    phase: float = 0.0,
    dark_bg: bool = True,
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

    if spec.get("adaptive_fill"):
        # the face follows the footage: white type on dark clips, near-black on bright ones
        fill_rgb = (255, 255, 255) if dark_bg else (16, 16, 18)
    else:
        fill_rgb = tuple(spec.get("fill") or (255, 255, 255))
    fill_col = (*fill_rgb, 255)
    stroke_col = (*tuple(spec.get("stroke_color") or (0, 0, 0)), 255)
    halo_rgb = (0, 0, 0) if sum(fill_rgb) > 380 else (255, 255, 255)   # halo opposes the fill

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if spec.get("gradient_stroke"):
        # ANIMATED GRADIENT STROKE: the outline is a moving gold sweep, so it can't be drawn by
        # the text call. Build it as a layer — outline-shaped mask (stroked glyphs MINUS filled
        # glyphs) punched out of a phase-shifted gold gradient — then the fill + emoji pass draws
        # over it. Emoji carry no stroke (they're images, not glyphs), which is what you want.
        outer = Image.new("L", (width, height), 0)
        inner = Image.new("L", (width, height), 0)
        do, di = ImageDraw.Draw(outer), ImageDraw.Draw(inner)
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            cy = top + i * step + line_h / 2.0
            do.text((width // 2, int(cy)), line, font=font, fill=255, anchor="mm",
                    stroke_width=stroke, stroke_fill=255)
            di.text((width // 2, int(cy)), line, font=font, fill=255, anchor="mm")
        ring = ImageChops.subtract(outer, inner)
        gold = _gold_layer(width, height, phase).convert("RGBA")
        gold.putalpha(ring)
        if spec["shadow"]:
            # a soft dark HALO, not an offset shadow: a deep-navy fill sits on dark footage, so
            # the glyphs need separation from behind rather than a drop shadow beside them.
            halo = Image.new("RGBA", (width, height), (*halo_rgb, 0))
            blur = outer.filter(ImageFilter.GaussianBlur(max(4, size // 8)))
            halo.putalpha(blur.point(lambda v: min(255, int(v * 1.9))))
            img.alpha_composite(halo)
        img.alpha_composite(gold)
        with Pilmoji(img, source=_AppleThenNotoSource) as pilmoji:
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                cy = top + i * step + line_h / 2.0
                pilmoji.text((width // 2, int(cy)), line, font=font, fill=fill_col,
                             anchor="mm", emoji_scale_factor=1.15)
        if spec.get("sphere"):
            img = _sphere_warp(img, float(spec["sphere"]))
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        img.save(out_path)
        return out_path

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
                                     fill=(0, 0, 0, 150) if pass_shadow else fill_col,
                                     anchor="lm",
                                     stroke_width=0 if pass_shadow else stroke,
                                     stroke_fill=stroke_col)
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
                fill=fill_col,
                anchor="mm",
                stroke_width=stroke,
                stroke_fill=stroke_col,
                emoji_scale_factor=1.15,
            )
    if spec.get("sphere"):
        img = _sphere_warp(img, float(spec["sphere"]))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)  # tmp/ may not exist on a fresh host
    img.save(out_path)
    return out_path


def render_caption_frames(text: str, out_path: str, font_style: str = "base", **kw) -> tuple[str, int]:  # noqa: D401
    """Render the caption for compositing. Static styles → (png_path, 0). Animated styles →
    (printf pattern of the frame sequence, fps) — the compositor loops the sequence over the
    reel, so the loop length is `frames / fps` seconds regardless of the reel's duration."""
    spec = _FONT_STYLES.get(font_style) or _FONT_STYLES["base"]
    n = int(spec.get("frames") or 0)
    fps = int(spec.get("fps") or 0)
    if not (spec.get("gradient_stroke") and n > 1 and fps > 0):
        return render_caption_png(text, out_path, font_style=font_style, **kw), 0
    stem = os.path.splitext(out_path)[0]
    for i in range(n):
        render_caption_png(text, f"{stem}_f{i:03d}.png", font_style=font_style,
                           phase=i / float(n), **kw)
    return f"{stem}_f%03d.png", fps
