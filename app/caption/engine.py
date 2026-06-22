"""Corpus-driven caption engine — Layer 1 of the learning loop (RAG generation).

Retrieves real reference captions from the creator's corpus and generates NEW candidates in
that voice, conditioned on the chosen audio's archetype profile (and clip context if any).
The static few-shot is gone; the corpus is the live knowledge.
"""
from __future__ import annotations

import json

from anthropic import Anthropic

from app.config import settings
from app.corpus.store import load_refs, retrieve

# Soft affinity: an audio's PURPOSE -> the persona modes that tend to fit it. Used to bias
# retrieval, not to hard-restrict. (Captions are not bound to a track — they pair on vibe.)
_AUDIO_MODE_AFFINITY = {
    "reflective_glowup": ["deep_bro_sincere", "antimediocrity_dread", "deep_bro_provocative"],
    "villain_reveal": ["shameless_villain", "self_aware_villain", "absurd_villain", "ego_wordplay_villain"],
    "flex_montage": ["shameless_villain", "self_aware_hustler", "ego_wordplay_villain", "anticope_callout"],
    "contrarian_rant": ["anti_simp", "shameless_contrarian", "anticope_callout", "deadpan_possessive"],
    "comedic_bait": ["deadpan_crude", "absurd_villain", "antideep_parody", "self_aware_absurd_flex", "backhanded_deadpan"],
    "relatable_confession": ["self_aware_hustler", "anti_simp"],
    "stats_gutpunch": ["anticope_callout", "deep_bro_provocative", "antimediocrity_dread"],
}

_SYS = """You write short-form captions in ONE specific creator's voice. The caption IS the post — the words carry it; a clip plays behind. Goal: something a very-online person screenshots and SENDS to a friend (shareability is the dominant lever in this creator's corpus).

You are given REAL reference captions from THIS creator's corpus, each with WHY it works. Study the voice, the persona modes, and the mechanics — then write NEW captions with the same energy. Do NOT copy or lightly reword them; bring fresh topics and angles.

Rules learned the hard way:
- FUNNY or genuinely insightful first. Decode the real mechanism — never write something that merely sounds edgy or deep.
- The voice is DUAL: shameless-funny (villain / anti-simp / absurd / crude / ego-wordplay) AND sincere-mentor motivation (proverbs, reps-make-mastery, grind-dread). Match what the audio calls for.
- Most lines are UNIVERSAL (core persona) — do NOT force a niche/theme. lowercase-leaning, deadpan, very-online slang; emojis are fine (😭🙏🥷); a little mean is good.
- Commit each caption to ONE move + the 1-2 levers it nails. Don't cram every variable in (it comes out lame).
- Specific over abstract; never end on a vague concept.
{clip_line}

Return ONLY JSON, no prose:
{{"candidates": [{{"text": "the caption (\\n for line breaks)", "mode": "persona mode", "primary_lever": "shareability|comment_bait|relatability|iykyk_decode|shock_humor|...", "why": "one line on the mechanism"}}]}}"""


def _client() -> Anthropic:
    return Anthropic(api_key=settings.anthropic_api_key)


def _modes_for_audio(audio_purpose: list[str] | None) -> list[str]:
    modes: list[str] = []
    for p in audio_purpose or []:
        modes += _AUDIO_MODE_AFFINITY.get(p, [])
    return list(dict.fromkeys(modes))  # dedup, preserve order


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 6,
    clip_context: str | None = None,
    exclude_captions: list[str] | None = None,
) -> list[dict]:
    """Generate n candidate captions conditioned on the corpus + the audio profile."""
    target_modes = _modes_for_audio(audio_purpose)
    chosen = retrieve(target_modes=target_modes, n=12, exclude_captions=exclude_captions)

    ref_lines = [
        '- "%s"  [%s] — %s' % (r.get("caption", ""), r.get("persona_trait", ""), r.get("why_it_works", ""))
        for r in chosen
    ]
    ref_block = "\n".join(ref_lines) or "(corpus empty)"

    if clip_context:
        clip_line = (
            f"- This reel's footage: {clip_context}. Prefer captions that fit it; only write a "
            "reaction-bound caption if the footage is a candid reaction shot."
        )
    else:
        clip_line = (
            "- Footage is flexible flashy b-roll; avoid reaction-bound captions that need a specific shot."
        )

    sys = _SYS.format(clip_line=clip_line)
    user = (
        f"AUDIO — vibe: {audio_vibe}; purpose: {audio_purpose}; energy: {audio_energy}.\n"
        f"Creator notes/topic (optional): {notes or 'none — lean core persona, any topic'}.\n\n"
        f"REFERENCE CORPUS (match the voice, do NOT copy):\n{ref_block}\n\n"
        f"Write {n} DISTINCT candidates — span different persona modes (mix funny and sincere-"
        f"motivational as the audio fits), different topics. Funny/insightful first, built to be SENT."
    )

    msg = _client().messages.create(
        model=settings.caption_model,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=sys,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start : end + 1]).get("candidates", [])[:n]
    except json.JSONDecodeError:
        return []
