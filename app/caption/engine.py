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

_SYS = """You write short-form captions AS ONE specific creator. Below are REAL captions of theirs — this IS the voice. Study EVERYTHING: the exact language and slang, the FORMATTING (line breaks, length), the wild hyper-specificity (named brands, exact dollar amounts, vbucks / parlays / streamers / blackjack), the degenerate gambling + crude + shock humor, the anti-motivational SUBVERSIONS, the very-online references. Then write NEW ones that are JUST as unhinged and specific.

THE #1 FAILURE is writing a clean, safe, GENERIC version of the vibe — a smooth "money mindset" motivational-clapback that sounds like every other hustle account. These references are weird, hyper-specific, degenerate, and shocking. Do NOT sanitize them: name the exact thing, go to the dark/crude/absurd place, subvert the motivational setup. Be as specific and unhinged as they are.

- Match their FORMATTING: multi-line with line breaks when they do it (\\n / \\n\\n), dead-simple one-liner when they do that. Lowercase-leaning, very-online.
- Don't copy or reword any reference — fresh topics/angles. Don't rehash any exact line in the AVOID list (reusing a setup with a genuinely new joke is fine).
{clip_line}

Return ONLY JSON, no prose:
{{"candidates": [{{"text": "the caption (use \\n for line breaks)", "mode": "short label", "primary_lever": "shareability|comment_bait|relatability|iykyk_decode|shock_humor|...", "why": "one line"}}]}}"""


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
    pool = retrieve(refs, target_modes=target_modes, n=min(40, max(1, len(refs))))
    random.shuffle(pool)

    # The voice = the creator's REAL captions ONLY — a big sample with FORMATTING (line breaks)
    # intact. Do NOT mix in generated keeps/bests: they're smoother than the references and anchor
    # the model to a sanitized version of the voice.
    gold = [(r.get("caption") or "").strip() for r in pool[:40] if (r.get("caption") or "").strip()]
    gold_block = "\n\n".join(f"[{i + 1}]\n{c}" for i, c in enumerate(gold)) or "(corpus empty)"

    # Only recent GENERATIONS go here (to avoid rehashing exact lines). Kills are deliberately NOT
    # used in-context: the same setup gets both kept AND killed (execution-dependent), so a kill is
    # too noisy to steer the voice — using it bans good setups and drifts the voice weak/weird.
    # Kills still feed the future reward model, where the noise averages out across many examples.
    avoid = recent_generated(50)
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
        f"REAL CAPTIONS FROM THIS CREATOR ({len(gold)} of them) — THIS is the voice. Match the language, slang, "
        "formatting, specificity, and unhinged energy; write new ones that could sit in this exact list unnoticed:\n\n"
        f"{gold_block}\n\n"
        f"RECENTLY SHOWN — don't rehash these exact lines (a fresh joke on a similar setup is fine):\n{avoid_block}\n\n"
        f"Audio vibe: {audio_vibe or 'n/a'} ({audio_energy or ''}). Notes: {notes or 'none'}.\n"
        f"Write {n} new captions in this voice. Span the range so no two are alike. Be as SPECIFIC and UNHINGED as "
        "the references — the worst thing you can do is write a clean, generic, safe version. Match their formatting."
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
