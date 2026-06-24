"""Caption engine — reference-dominated, full-range, SPARK-PAIRED.

Understanding (real re-read of all 95 refs): the voice is a precise TWIST stated with deadpan
confidence, hyper-specific + very-online, spanning a huge range (wordplay, analogy, would-you-rather,
anti-cope, sharp sincere reframes, villain flex, anti-simp, observational-absurd, grind-dread,
self-own, crude, degenerate). Grading (101 verdicts) showed that one-shot "write N varied captions"
OVER-produces three things — gambling volume, corny poster-metaphors, and template xerox — all from
the SAME root: a single call mirrors the corpus's gambling weight, grooves on one template and
photocopies it, and defaults corny when it reaches for "sincere".

The fix is STRUCTURAL, not a pile of "don't" notes (every enforcement attempt this project degraded
the voice). Each slot in the batch is PAIRED to a different real reference as an energy-SPARK, and
the sparks are chosen ONE-PER-DISTINCT-TRAIT (the batch spans the range by construction) with
gambling bounded to ~one spark (one color, not the painting). Independent spark per slot => no
template xerox; sparks grounded in real sharp captions => no vague-"sincere"-gone-corny. The whole
corpus still shows for voice; the sparks steer the OUTPUT distribution off the corpus's gambling
weight. No caps on the voice, no move-decomposition, no judge. The grading loop does the curation.
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
- THE FULL RANGE. You are NOT one note. Crude shock, clean wordplay, villain flex, anti-cope, GENUINELY sharp sincere truths (the kind that are SPECIFIC and real — "nobody is good at the start, nobody is bad after 1000 attempts" — never a soft nature-metaphor about seeds or rivers), existential grind-dread, money-bravado. Each lives in its own corner.

You write with a precise twist, deadpan confidence, and hyper-specific very-online detail — never approximate, never corny, never a stretched-out story when one beat would kill."""


def _pick_sparks(refs: list[dict], n: int) -> list[dict]:
    """One spark per distinct persona_trait (spans the range), gambling bounded to ~one spark."""
    by_trait: dict[str, list[dict]] = {}
    for r in refs:
        if (r.get("caption") or "").strip():
            by_trait.setdefault(r.get("persona_trait") or "?", []).append(r)
    traits = list(by_trait)
    random.shuffle(traits)
    sparks: list[dict] = []
    gambling_used = 0
    for t in traits:
        if len(sparks) >= n:
            break
        r = random.choice(by_trait[t])
        if _is_gambling(r):
            if gambling_used >= 1:
                continue
            gambling_used += 1
        sparks.append(r)
    if len(sparks) < n:  # not enough distinct traits — top up with any unused refs
        chosen = {id(s) for s in sparks}
        pool = [r for r in refs if id(r) not in chosen and (r.get("caption") or "").strip()]
        random.shuffle(pool)
        sparks += pool[: n - len(sparks)]
    random.shuffle(sparks)
    return sparks[:n]


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Spark-paired, full-range generation. Each slot riffs off a distinct-trait real reference."""
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    sparks = _pick_sparks(refs, n)
    spark_block = "\n\n".join(
        f"SPARK {i + 1}: {(s.get('caption') or '').strip()}" for i, s in enumerate(sparks)
    )
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Tonight you're writing ALONGSIDE this spread of your own captions — each one a different "
        "flavor of you. For EACH spark, write ONE fresh caption that carries the same ENERGY and SHAPE "
        "but a completely different subject — never a rewrite or near-version of the spark itself:\n\n"
        + spark_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per spark, in order. ONLY JSON, no prose: "
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
