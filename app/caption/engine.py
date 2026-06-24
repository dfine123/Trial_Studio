"""Caption engine — reference-dominated, full-range, ROTATION-anchored for format coverage + variety.

Understanding (real re-read of all 95 refs): the voice is a precise TWIST, deadpan, hyper-specific +
very-online, spanning a HUGE range of distinct FORMATS (🥷 hate-so-much, the Iran/I-ran homophone,
for-perspective scale parody, the Zuckerberg-timeline, Uber/Amazon middlemen, would-you-rather,
two-speaker, 50/30/20 rule, objects-in-mirror, fake-stat, X-is-like-Y, proverb-subversion, ...).
The references' POWER is that variety.

Diagnosis (graded session on the trait-mold engine): organizing slots by persona_trait (TONE) was
the bug — tone != format, some traits map to ONE rigid format, and random trait-selection repeated a
handful ("How I look at [relative]" ×4, "you let your girl" ×3, crime-wordplay ×3) while ~15 real
formats never appeared. Framing felt constant; formats were missing.

Fix (structural): anchor each slot to a DISTINCT real reference and ROTATE through the whole corpus —
a persisted usage tracker picks least-used-first, so EVERY format gets its turn and nothing repeats
until the set is cycled. One sharp anchor per slot (distinct trait within a batch for tonal spread,
gambling soft-capped). Whole corpus still shown for voice. No trait-molds, no caps on the voice,
no judge. The grading loop curates.
"""
from __future__ import annotations

import json
import os
import random

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs

_GAMBLING_TERMS = (
    "parlay", "casino", "blackjack", "dealer", "slot", "sportsbook", "vegas", "lottery",
    "gambl", "on black", "on red", "the odds", "comp room", "referral code", "the under",
    "the over", "betting", "a bet", "rimmed out", "put $", "down bad on this hand", "the hand",
    "card declined", "deposit", "hit me", "hitting is", "ante", "roulette",
)
_REF_USAGE_PATH = os.path.join("var", "ref_usage.json")


def _is_gambling(r: dict) -> bool:
    if r.get("persona_trait") == "self_aware_degenerate":
        return True
    cap = (r.get("caption") or "").lower()
    return any(t in cap for t in _GAMBLING_TERMS)


def _ref_key(r: dict) -> str:
    return r.get("ref_id") or (r.get("caption") or "")[:60]


def _load_ref_usage() -> dict:
    try:
        with open(_REF_USAGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_ref_usage(usage: dict) -> None:
    os.makedirs("var", exist_ok=True)
    tmp = _REF_USAGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(usage, f)
    os.replace(tmp, _REF_USAGE_PATH)


_SYS = """You ARE this creator, writing your own short-form captions — the kind people screenshot and send a friend. Below is a big pile of your REAL captions. This is the voice, the range, AND the bar:

{references}

What every one of these shares (your instincts — feel them, don't check them off):
- THE TWIST. The setup primes one thing; the line flips to another — the GAP is the joke. A homophone decode ("Iran this, Iran that" -> "I ran up a bag"), a reframe ("we ain't broke, we pre-rich"), a bait-and-switch ("I bet you have hoes / ahh so close, I have a gambling problem"), a self-own ("you're broke because you don't work, I'm broke because I make bad financial decisions — we are not the same").
- PRECISION. The twist maps EXACTLY. "A fat chick saying she has big boobs is like an unemployed dude saying he has a day off" lands because the two map perfectly. Approximate or almost-funny is dead.
- DEADPAN CONFIDENCE. Stated flat, like it's obvious, even when it's unhinged ("be more like a crackhead").
- HYPER-SPECIFIC + VERY-ONLINE. Real specifics (vbucks, Adin Ross, 1099 vs W-2, a $200 casino trip), real slang (bro, ahh, fym, 🥷, "broke ahh"), emoji when it lands.
- ALWAYS SHARP — never generic. Even your sincere lines are SPECIFIC truths or parody ("nobody is good at the start, nobody is bad after 1000 attempts"). You never sound like a motivational poster, a quote everyone's heard, or a soft nature-metaphor about seeds and rivers — that's the one thing that is never you."""


def _pick_anchors(refs: list[dict], n: int) -> list[dict]:
    """n DISTINCT reference anchors, least-used-first (rotates through the whole corpus so every
    format gets covered), distinct persona_trait within a batch for tonal spread, gambling soft-cap."""
    usage = _load_ref_usage()
    pool = [r for r in refs if (r.get("caption") or "").strip()]
    random.shuffle(pool)  # random tiebreak among equally-used refs
    pool.sort(key=lambda r: usage.get(_ref_key(r), 0))  # least-used first
    anchors: list[dict] = []
    seen_traits: set[str] = set()
    gambling = 0
    for r in pool:
        if len(anchors) >= n:
            break
        trait = r.get("persona_trait") or "?"
        if trait in seen_traits:  # one per trait this batch -> tonal + format spread
            continue
        if _is_gambling(r):
            if gambling >= 2:
                continue
            gambling += 1
        anchors.append(r)
        seen_traits.add(trait)
    if len(anchors) < n:  # ran out of distinct traits — relax the constraint, keep rotating
        chosen = {id(a) for a in anchors}
        for r in pool:
            if len(anchors) >= n:
                break
            if id(r) not in chosen:
                anchors.append(r)
                chosen.add(id(r))
    for r in anchors:
        usage[_ref_key(r)] = usage.get(_ref_key(r), 0) + 1
    _save_ref_usage(usage)
    random.shuffle(anchors)
    return anchors[:n]


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Rotation-anchored generation: each slot writes a fresh caption in the FORMAT of a distinct
    real reference, rotating through the whole corpus for full format coverage + variety."""
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    anchors = _pick_anchors(refs, n)
    anchor_block = "\n\n".join(
        f"ANCHOR {i + 1}: {(a.get('caption') or '').strip()}" for i, a in enumerate(anchors)
    )
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Here are " + str(n) + " of your own real captions — each one a DIFFERENT format you use. "
        "For EACH anchor, write ONE NEW caption in that SAME format and voice: the same structure, "
        "rhythm, length, and kind of twist — but a totally fresh subject (never a rewrite of its "
        "joke). Match its exact sharpness and hyper-specificity; if yours lands softer or vaguer than "
        "the anchor, it's not there yet.\n\n"
        + anchor_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per anchor, in order. ONLY JSON, no prose: "
        '{"candidates": [{"text": "the caption (\\n for line breaks)"}]}'
    )
    text = complete_json(_SYS.format(references=ref_block), user, effort="high", max_tokens=4000)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        cands = json.loads(text[start:end + 1]).get("candidates", [])
    except json.JSONDecodeError:
        return []
    out = [c for c in cands[:n] if isinstance(c, dict) and (c.get("text") or "").strip()]
    out = refine(out)
    log_generated([c.get("text", "") for c in out])
    return out
