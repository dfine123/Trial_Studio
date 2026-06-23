"""Corpus-driven caption engine — Layer 1 (RAG generation).

The voice lives in the creator's REAL captions, so generation is grounded in a big RAW set of
them (the gathered references + the creator's bests/keeps), shown as plain text with NO analysis
labels — the model pattern-matches the actual texture instead of following a wall of rules. The
instructions are deliberately light. Grades + the recent-generation log feed back in-context
(match keeps, avoid kills/repeats); a separate refine layer trims over-extended tails.
"""
from __future__ import annotations

import json
import random

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.grades import best_captions, kept_captions, killed_captions
from app.corpus.store import load_refs, retrieve

# Soft affinity: an audio's PURPOSE -> persona modes that tend to fit it (biases retrieval only).
_AUDIO_MODE_AFFINITY = {
    "reflective_glowup": ["deep_bro_sincere", "antimediocrity_dread", "deep_bro_provocative"],
    "villain_reveal": ["shameless_villain", "self_aware_villain", "absurd_villain", "ego_wordplay_villain"],
    "flex_montage": ["shameless_villain", "self_aware_hustler", "ego_wordplay_villain", "anticope_callout"],
    "contrarian_rant": ["anti_simp", "shameless_contrarian", "anticope_callout", "deadpan_possessive"],
    "comedic_bait": ["deadpan_crude", "absurd_villain", "antideep_parody", "self_aware_absurd_flex", "backhanded_deadpan"],
    "relatable_confession": ["self_aware_hustler", "anti_simp"],
    "stats_gutpunch": ["anticope_callout", "deep_bro_provocative", "antimediocrity_dread"],
}

_SYS = """You write short-form captions AS ONE specific creator. The REAL captions you're shown ARE the voice — study their exact language, slang, rhythm, length, and attitude, then write NEW ones that sound like the same person wrote them. Match the TEXTURE, not a formula.

- Talk like them: raw, blunt, lowercase, deadpan, very-online slang (mf, fym, ahh, "broke ahh", dat, ik, ts). a little mean.
- Many of their best are DEAD SIMPLE — pure raw attitude, no clever wordplay ("wtf is budget... just make more money pussy"). Don't get writerly or over-construct; the funniest hit fast and land on something concrete.
- Steal their cadence and slang, not their topics. Never corny, sentimental, soft, or motivational-poster.
- Don't copy or reword any example — fresh topics/angles. Never repeat anything in the AVOID list.
{clip_line}

Return ONLY JSON, no prose:
{{"candidates": [{{"text": "the caption (\\n for line breaks)", "mode": "short label", "primary_lever": "shareability|comment_bait|relatability|iykyk_decode|shock_humor|...", "why": "one line"}}]}}"""


def _modes_for_audio(audio_purpose: list[str] | None) -> list[str]:
    modes: list[str] = []
    for p in audio_purpose or []:
        modes += _AUDIO_MODE_AFFINITY.get(p, [])
    return list(dict.fromkeys(modes))


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Generate n captions grounded in a big RAW set of the creator's real captions + grades."""
    refs = load_refs()
    target_modes = _modes_for_audio(audio_purpose)
    pool = retrieve(refs, target_modes=target_modes, n=min(30, max(1, len(refs))))
    random.shuffle(pool)

    # The voice = the real captions. Lead with the gathered references (the actual creator voice),
    # then the crowned bests + recent keeps. Raw text, no analysis labels, deduped.
    gold, seen = [], set()
    for c in [r.get("caption", "") for r in pool[:26]] + best_captions()[-6:] + kept_captions()[-8:]:
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            gold.append(c)
    gold_block = "\n".join("- " + c.replace("\n", " / ") for c in gold) or "(corpus empty)"

    avoid = (killed_captions() + recent_generated(45))[-60:]
    avoid_block = "\n".join("- " + c.replace("\n", " / ") for c in avoid) or "(none yet)"

    if clip_context:
        clip_line = (
            f"- This reel's footage: {clip_context}. Prefer captions that fit it; only write a "
            "reaction-bound caption if the footage is a candid reaction shot."
        )
    else:
        clip_line = "- Footage is flexible flashy b-roll; avoid reaction-bound captions that need a specific shot."

    sys = _SYS.format(clip_line=clip_line)
    user = (
        "REAL CAPTIONS FROM THIS CREATOR — this IS the voice. Match their language, slang, cadence, "
        "length, and attitude; write new ones that sound like the same person. Do NOT copy or reword any:\n"
        f"{gold_block}\n\n"
        f"AVOID — already shown or rejected; never repeat, reword, or reuse the structure of any:\n{avoid_block}\n\n"
        f"Audio vibe: {audio_vibe or 'n/a'} ({audio_energy or ''}). Notes: {notes or 'none'}.\n"
        f"Write {n} new captions. Span topics so no two are alike — money / the grind shows up a lot (their "
        "world) but not every line; at most one sincere-motivational. Funny/sharp first, RAW not constructed; "
        "never corny. None may echo AVOID."
    )

    text = complete_json(sys, user, effort="high", max_tokens=4000)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        cands = json.loads(text[start : end + 1]).get("candidates", [])[:n]
    except json.JSONDecodeError:
        return []
    cands = refine(cands)
    log_generated([c.get("text", "") for c in cands])
    return cands
