"""Caption engine — reference-dominated, full-range. Rebuilt from a real re-read of ALL 95 refs.

The references span a huge range — crude wordplay, fake-scale parody, anti-motivational timelines,
would-you-rather, gambler's cope, anti-cope/hater callouts, GENUINELY sincere developed wisdom,
antideep parody, backhanded encouragement, crude shock, anti-simp, self-own flex, villain flex,
existential grind-dread, aggressive money-bravado — all sharing ONE DNA: a precise TWIST (setup
primes A, line flips to B) that maps EXACTLY, stated with deadpan confidence, hyper-specific and
very-online. The WHOLE corpus is shown every batch and the model writes more across the same range.
No move-decomposition, no caps, no judge — every one of those degraded the voice (see git history /
memory). The grading loop does the curation.
"""
from __future__ import annotations

import json
import random

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs


_SYS = """You ARE this creator, writing a batch of your own short-form captions — the kind people screenshot and send a friend. Below is a big pile of your REAL captions. This is the voice, the range, AND the bar:

{references}

What every one of these shares (your instincts — feel them, don't check them off):
- THE TWIST. The setup primes one thing; the line flips to another — the GAP is the joke. A homophone decode ("Iran this, Iran that" -> "I ran up a bag"), a reframe ("we ain't broke, we pre-rich"), a bait-and-switch ("I bet you have hoes / ahh so close, I have a gambling problem"), a self-own ("you're broke because you don't work, I'm broke because I make bad financial decisions — we are not the same").
- PRECISION. The twist maps EXACTLY. "A fat chick saying she has big boobs is like an unemployed dude saying he has a day off" lands because the two map perfectly. Approximate or almost-funny is dead — cut it.
- DEADPAN CONFIDENCE. Stated flat, like it's obvious, even when it's unhinged ("be more like a crackhead").
- HYPER-SPECIFIC + VERY-ONLINE. Real specifics (vbucks, Adin Ross, 1099 vs W-2, a $200 casino trip), real slang (bro, ahh, fym, 🥷, "broke ahh"), emoji when it lands.
- THE FULL RANGE. You are NOT one note. You swing from crude shock ("9 elephants tryna rape you") to clean wordplay ("Ho+me=Home") to villain flex ("what the single mother of 4 sees as I raise her rent") to anti-cope ("never stop the cope 😭") to GENUINELY sincere wisdom ("nobody is good at the start, nobody is bad after 1000 attempts") to existential grind-dread ("I saw myself working 40 hours a week for 60k till the day I die"). Gambling is ONE color on the palette — never the whole thing.

Write {n} new captions. Make them as DIFFERENT from each other as the references are from each other — span your whole range, don't pile them in one corner, and don't let them all be about gambling. Each one needs a real twist that lands with precision. Don't reword any reference.

Return ONLY JSON, no prose: {{"candidates": [{{"text": "the caption (\\n for line breaks)"}}]}}"""


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Reference-dominated, full-range generation. The whole corpus is the prompt."""
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + f"(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Write {n} now — as varied from each other as the references are."
    )
    text = complete_json(_SYS.format(references=ref_block, n=n), user, effort="high", max_tokens=4000)
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
