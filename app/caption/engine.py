"""Caption engine — reference-dominated, full-range, MOLD-anchored for tight conformance.

Understanding (real re-read of all 95 refs): the voice is a precise TWIST stated with deadpan
confidence, hyper-specific + very-online, spanning a huge range. Always SHARP + specific or parody —
NEVER generic wisdom.

Diagnosis (fresh batches vs the references): FORMAT conformance is already high (outputs ride the
proven molds, sometimes near-reskins), but the "sincere/deep" slots DRIFT into generic motivation /
borrowed clichés the references never touch ("Rock bottom has a basement", "the last knock opens the
door") — because seeding a slot with ONE reference + "riff loose" lets the model freewheel on "deep".

Conformance fix (structural, NOT more "don't" notes): anchor each batch slot to a tight MOLD — a SET
of ~3 real references from the SAME pocket of the voice — and ask for one that slips into that exact
set unnoticed. Three real examples per slot pin the precise sharpness/rhythm, so the model imitates
the pocket instead of drifting generic. Molds are one-per-distinct-trait (spans the range), gambling
bounded to ~one mold. Whole corpus still shown for voice. No move-decomposition, no caps, no judge.
"""
from __future__ import annotations

import json
import random

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs

_GAMBLING_TERMS = (
    "parlay", "casino", "blackjack", "dealer", "slot", "sportsbook", "vegas", "lottery",
    "gambl", "on black", "on red", "the odds", "comp room", "referral code", "the under",
    "the over", "betting", "a bet", "rimmed out", "put $", "down bad on this hand", "the hand",
    "card declined", "deposit", "hit me", "hitting is", "ante",
)


def _is_gambling(r: dict) -> bool:
    if r.get("persona_trait") == "self_aware_degenerate":
        return True
    cap = (r.get("caption") or "").lower()
    return any(t in cap for t in _GAMBLING_TERMS)


_SYS = """You ARE this creator, writing your own short-form captions — the kind people screenshot and send a friend. Below is a big pile of your REAL captions. This is the voice, the range, AND the bar:

{references}

What every one of these shares (your instincts — feel them, don't check them off):
- THE TWIST. The setup primes one thing; the line flips to another — the GAP is the joke. A homophone decode ("Iran this, Iran that" -> "I ran up a bag"), a reframe ("we ain't broke, we pre-rich"), a bait-and-switch ("I bet you have hoes / ahh so close, I have a gambling problem"), a self-own ("you're broke because you don't work, I'm broke because I make bad financial decisions — we are not the same").
- PRECISION. The twist maps EXACTLY. "A fat chick saying she has big boobs is like an unemployed dude saying he has a day off" lands because the two map perfectly. Approximate or almost-funny is dead.
- DEADPAN CONFIDENCE. Stated flat, like it's obvious, even when it's unhinged ("be more like a crackhead").
- HYPER-SPECIFIC + VERY-ONLINE. Real specifics (vbucks, Adin Ross, 1099 vs W-2, a $200 casino trip), real slang (bro, ahh, fym, 🥷, "broke ahh"), emoji when it lands.
- ALWAYS SHARP — never generic. Even your sincere lines are SPECIFIC truths or parody ("nobody is good at the start, nobody is bad after 1000 attempts"). You never sound like a motivational poster, a quote everyone's heard, or a soft nature-metaphor about seeds and rivers — that's the one thing that is never you."""


def _pick_molds(refs: list[dict], n: int) -> list[list[dict]]:
    """n MOLDS — one per distinct persona_trait (spans the range), each a set of up to 3 real refs
    from that same pocket. Gambling bounded to ~one mold."""
    by_trait: dict[str, list[dict]] = {}
    for r in refs:
        if (r.get("caption") or "").strip():
            by_trait.setdefault(r.get("persona_trait") or "?", []).append(r)
    traits = list(by_trait)
    random.shuffle(traits)
    molds: list[list[dict]] = []
    gambling_used = 0
    for t in traits:
        if len(molds) >= n:
            break
        group = list(by_trait[t])
        random.shuffle(group)
        mold = group[:3]
        if sum(1 for r in mold if _is_gambling(r)) > len(mold) / 2:  # predominantly gambling pocket
            if gambling_used >= 1:
                continue
            gambling_used += 1
        molds.append(mold)
    if len(molds) < n:  # not enough distinct traits — pad with extra single-ref molds
        used = {id(r) for m in molds for r in m}
        pool = [r for r in refs if id(r) not in used and (r.get("caption") or "").strip()]
        random.shuffle(pool)
        molds += [[r] for r in pool[: n - len(molds)]]
    random.shuffle(molds)
    return molds[:n]


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Mold-anchored generation: each slot writes one that slips into a tight set of 3 real refs."""
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    molds = _pick_molds(refs, n)
    mold_block = "\n\n".join(
        f"SET {i + 1}:\n" + "\n".join(f"  • {(r.get('caption') or '').strip()}" for r in mold)
        for i, mold in enumerate(molds)
    )
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Here are " + str(n) + " small SETS of your own real captions — each set is one tight pocket "
        "of your voice. For EACH set, write ONE NEW caption that could slip into that exact set "
        "unnoticed: the same voice, the same sharpness, the same hyper-specific edge, the same rhythm "
        "and length — fresh material, but unmistakably from the same person. Never a rewrite of any "
        "line shown.\n\n"
        + mold_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per set, in order. ONLY JSON, no prose: "
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
