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

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.config import settings
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.grades import best_captions, kept_captions, killed_captions
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
    "BLUNT aggressive money-bravado — sneer at budgeting/saving as weak ('wtf is budget, just make more money'), cocky insult cap ('pussy', 'broke ahh', 'soft'); ATTITUDE over cleverness",
    "money / grindset — the come-up, out-earning, 'we gonna eat', work-ethic flex",
    "wealth mindset — rich-vs-poor mindset, opportunity, money psychology (sharp, never corny)",
    "finance / investing flex or wordplay (the S&P, the portfolio, the index-fund bit)",
    "absurd / villain / shameless humor, money-flavored (the landlord, charging people, the bill)",
    "self-aware degenerate (gambling, the bank app, the slot machine, the streets)",
    "crude / wordplay / IYKYK humor",
    "balanced — LEAD with money/grind; relationship/anti-simp at most a minority",
]

_SYS = """You write short-form captions in ONE specific creator's voice. The caption IS the post — the words carry it; a clip plays behind. Goal: something a very-online person screenshots and SENDS to a friend (shareability is the dominant lever in this creator's corpus).

You are given REAL reference captions from THIS creator's corpus, each with WHY it works. Study the voice, the persona modes, and the mechanics — then write NEW captions with the same energy. Do NOT copy or lightly reword them; bring fresh topics and angles.

Rules learned the hard way:
- BASE PERSONA: the narrator is a rich, winning, flex entrepreneur — the guy with the money, the landlord collecting rent, the one who already made it. Even the jokes come from that POV (villain landlord, the guy who charges his own therapist, the winner looking down). Flex/status is the bedrock under every caption; humor and motivation sit ON TOP of it.
- CORE SUBJECT = MONEY. This persona's home turf is making money: the come-up, out-earning everyone, rich-vs-poor mindset, investing/finance, opportunity, "we gonna eat", the grind. THAT is the main course. Relationship / anti-simp / "she said" / girlfriend jokes are good but a SIDE DISH — keep them a clear minority, never the dominant theme.
- MONEY TONE — don't over-polish. A CORE, under-used flavor is BLUNT AGGRESSIVE money-bravado: sneer at budgeting / saving / being careful with money as weak ("wtf is budget, just make more money"), prescribe earning over saving, cap with a cocky/hostile insult ("pussy", "broke ahh", "soft", "weird ahh"). Here the ATTITUDE is the payoff — NOT a clever twist. Not every line needs wordplay or a setup→reveal; sometimes raw dismissive dominance hits hardest. Stay cocky, blunt, a little hostile.
- NO CORNY / SENTIMENTAL / PSEUDO-POETIC lines — this is the most common cringe and an instant fail. Kill on sight: smug pet-names ("sweetheart", "darling"), flowery emotional payoffs, and any attempt to sound deep-and-SOFT about money. EXACT cringe to never write: "my bank keeps flagging the deposits as suspicious activity. that's not fraud sweetheart. that's just the first time the account's seen someone actually mean it." — theatrical, soft, self-serious, trying to be poetic. Heuristic: if a line could be read aloud in a wistful, tender voice, it FAILED. Blunt and a little mean ALWAYS beats poetic and soft.
- LEAD WITH FUNNY. The creator wants genuinely funny captions MORE than motivational ones — but funny only counts if the payoff lands.
- THE PAYOFF IS EVERYTHING. The #1 failure mode is a strong setup with a limp, confusing, or illogical payoff. The punchline must hit hard, be specific, and be logically airtight — the premise has to actually hold (no logic holes like "a funeral is invite-only anyway", no weak analogies, nothing corny or try-hard). A great setup with a weak payoff is a FAILURE — rebuild the landing or throw the whole line out.
- Decode the real mechanism — never write something that merely sounds edgy or deep.
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

    best = best_captions()[-8:]
    kept = kept_captions()[-12:]
    avoid = (killed_captions() + recent_generated(45))[-60:]

    ref_lines = [
        '- "%s"  [%s] — %s' % (r.get("caption", ""), r.get("persona_trait", ""), r.get("why_it_works", ""))
        for r in chosen
    ]
    ref_block = "\n".join(ref_lines) or "(corpus empty)"
    best_block = "\n".join("- " + c.replace("\n", " / ") for c in best) or "(none yet)"
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
        f"CROWNED BEST — the creator's all-time favorites; THIS is the bar every caption must clear:\n{best_block}\n\n"
        f"GOOD — these LANDED with the creator (match this caliber and spirit, never copy):\n{good_block}\n\n"
        f"AVOID — already shown or rejected. NEVER repeat, reword, or reuse the structure/template of any:\n{avoid_block}\n\n"
        f"This batch: {focus}. Write {n} captions. Center the THEME on MONEY / the grind / wealth / finance (this "
        f"persona's world) — keep relationship/anti-simp to AT MOST 1 of the batch, not the theme. MOST must be "
        f"genuinely FUNNY (lead with humor); a couple may be sincere money/grind motivation. Every one must be a "
        f"DIFFERENT structure and opening — NO two in this batch share a template or mold, none echo the AVOID list. "
        f"Each must land a hard, coherent payoff (logic must hold; no weak analogies, nothing corny) and be strong "
        f"enough you'd stake your name on it; if the landing is weak, confusing, or familiar, throw it out and rebuild. Built to be SENT."
    )

    text = complete_json(sys, user, effort="high", max_tokens=4000)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        cands = json.loads(text[start : end + 1]).get("candidates", [])[:n]
    except json.JSONDecodeError:
        return []
    cands = refine(cands)  # separate editor layer: trims over-extended / corny tails (only cuts)
    log_generated([c.get("text", "") for c in cands])
    return cands
