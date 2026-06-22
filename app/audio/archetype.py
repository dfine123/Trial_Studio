"""Audio archetype — the abstraction captions pair to.

Captions are NOT tied to a specific track. They pair to an audio's PROFILE = vibe (sonic mood) +
purpose (the narrative/caption move it best carries) + energy + tempo. New audios get classified
into the same fixed vocabulary, so they immediately inherit every reference caption tied to that
profile. The corpus + retrieval + generation all key off this profile, never the audio id.
"""
from __future__ import annotations

import json

from anthropic import Anthropic

from app.config import settings

# Controlled vocabulary — the whole point is a small, stable space every audio maps into.
VIBES = [
    "slowed_ethereal", "dark_menacing", "aggressive_gritty", "hype_celebratory",
    "punchy_spoken", "bright_aspirational", "smooth_confident", "emotional_nostalgic",
]
PURPOSES = [
    "villain_reveal", "reflective_glowup", "comedic_bait", "flex_montage",
    "contrarian_rant", "relatable_confession", "stats_gutpunch",
]
ENERGY = ["low", "mid", "high", "rising"]


def tempo_band(bpm: float | None) -> str:
    if not bpm:
        return "unknown"
    if bpm < 90:
        return "chill"
    if bpm <= 120:
        return "mid"
    return "fast"


_SYS = """You classify a short-form audio into a FIXED archetype vocabulary so captions pair to its vibe + purpose, not the specific track. Choose ONLY from the allowed values.

vibe (pick 1-2 — the sonic mood): {vibes}
purpose (pick 1-3 — the caption/narrative moves this audio best carries): {purposes}
energy (pick 1): {energy}

Return ONLY JSON, no prose:
{{"vibe": ["..."], "purpose": ["..."], "energy": "...", "label": "3-4 word human label", "rationale": "one line"}}"""


def classify(
    description: str | None,
    thematic_tags: list[str] | None,
    energy_arc: str | None,
    bpm: float | None,
    structure: str | None,
) -> dict:
    """Map an audio's features to an archetype profile (vibe + purpose + energy + tempo)."""
    sys = _SYS.format(vibes=", ".join(VIBES), purposes=", ".join(PURPOSES), energy=", ".join(ENERGY))
    user = (
        f"description: {description}\n"
        f"thematic_tags: {thematic_tags}\n"
        f"energy_arc: {energy_arc}\n"
        f"bpm: {bpm}\n"
        f"structure: {structure}"
    )
    msg = Anthropic(api_key=settings.anthropic_api_key).messages.create(
        model=settings.caption_model,
        max_tokens=1200,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=sys,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start : end + 1]) if start != -1 else {}
    data["tempo_band"] = tempo_band(bpm)
    return data
