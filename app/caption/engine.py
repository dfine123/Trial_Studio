"""Corpus-driven caption engine — Layer 1 of the learning loop (RAG generation).

Retrieves real reference captions from the creator's corpus and generates NEW candidates in
that voice. Closes the loop in-context: feeds back the creator's KEEPS (match this caliber),
KILLS + recent generations (never repeat/reword/reuse), rotates the references, and varies the
focus each batch — so it stops regurgitating the same molds across batches.
"""
from __future__ import annotations

import json
import random

from anthropic import Anthropic

from app.config import settings
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.grades import kept_captions, killed_captions
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

_FOCUS = [
    "lean into the absurd / villain / shameless side",
    "lean into sincere mentor-motivation (proverbs, reps, grind-dread)",
    "lean anti-simp / relationship-contrarian",
    "lean self-aware hustler / grindset",
    "lean crude / wordplay / IYKYK",
    "mix modes widely — no two alike",
]

_SYS = """You write short-form captions in ONE specific creator's voice. The caption IS the post — the words carry it; a clip plays behind. Goal: something a very-online person screenshots and SENDS to a friend (shareability is the dominant lever in this creator's corpus).

You are given REAL reference captions from THIS creator's corpus, each with WHY it works. Study the voice, the persona modes, and the mechanics — then write NEW captions with the same energy. Do NOT copy or lightly reword them; bring fresh topics and angles.

Rules learned the hard way:
- FUNNY or genuinely insightful first. Decode the real mechanism — never write something that merely sounds edgy or deep.
- The voice is DUAL: shameless-funny (villain / anti-simp / absurd / crude / ego-wordplay) AND sincere-mentor motivation (proverbs, reps-make-mastery, grind-dread). Match what the audio calls for.
- Most lines are UNIVERSAL (core persona) — do NOT force a niche/theme. lowercase-leaning, deadpan, very-online slang; emojis are fine (😭🙏🥷); a little mean is good.
- Commit each caption to ONE move + the 1-2 levers it nails. Don't cram every variable in (it comes out lame).
- Specific over abstract; never end on a vague concept.
- VARY structure HARD — different openings, lengths, and shapes every time. Do NOT keep reaching for the same molds (e.g. "she said she wants a man who [X]\\nso i [Y] and left", "they ask how i [X]", endless "[X] = [Y]" lists). If a line feels like a template you've used before, break the mold or throw it out.
- You'll be given GOOD captions (match their caliber and spirit — never copy) and AVOID captions (already shown or rejected — never repeat, reword, or reuse their structure).
{clip_line}

Return ONLY JSON, no prose:
{{"candidates": [{{"text": "the caption (\\n for line breaks)", "mode": "persona mode", "primary_lever": "shareability|comment_bait|relatability|iykyk_decode|shock_humor|...", "why": "one line on the mechanism"}}]}}"""


def _client() -> Anthropic:
    return Anthropic(api_key=settings.anthropic_api_key, max_retries=5)


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
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Generate n candidate captions conditioned on the corpus, the creator's grades, and what's
    already been shown (so batches stay fresh and avoid rejected molds)."""
    refs = load_refs()
    target_modes = _modes_for_audio(audio_purpose)

    # Rotate references for cross-batch variety: pull a wide diverse set, shuffle, take a subset.
    pool = retrieve(refs, target_modes=target_modes, n=min(20, max(1, len(refs))))
    random.shuffle(pool)
    chosen = pool[:12]

    kept = kept_captions()[-12:]
    avoid = (killed_captions() + recent_generated(45))[-60:]

    ref_lines = [
        '- "%s"  [%s] — %s' % (r.get("caption", ""), r.get("persona_trait", ""), r.get("why_it_works", ""))
        for r in chosen
    ]
    ref_block = "\n".join(ref_lines) or "(corpus empty)"
    good_block = "\n".join("- " + c.replace("\n", " / ") for c in kept) or "(none graded yet)"
    avoid_block = "\n".join("- " + c.replace("\n", " / ") for c in avoid) or "(none yet)"

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
    focus = random.choice(_FOCUS)
    user = (
        f"AUDIO — vibe: {audio_vibe}; purpose: {audio_purpose}; energy: {audio_energy}.\n"
        f"Creator notes/topic (optional): {notes or 'none — lean core persona, any topic'}.\n\n"
        f"REFERENCE CORPUS (match the voice, do NOT copy):\n{ref_block}\n\n"
        f"GOOD — these LANDED with the creator (match this caliber and spirit, never copy):\n{good_block}\n\n"
        f"AVOID — already shown or rejected. NEVER repeat, reword, or reuse the structure/template of any:\n{avoid_block}\n\n"
        f"This batch: {focus}. Write {n} captions, each genuinely DISTINCT (different structure, topic, and "
        f"opening — no two share a template, none echo the AVOID list). Each must be strong enough you'd stake "
        f"your name on it; if an idea is weak or familiar, throw it out and write a better one. Funny/insightful first, built to be SENT."
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
        cands = json.loads(text[start : end + 1]).get("candidates", [])[:n]
    except json.JSONDecodeError:
        return []
    log_generated([c.get("text", "") for c in cands])
    return cands
