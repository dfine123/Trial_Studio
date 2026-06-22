"""Refinement layer — a ruthless editor pass that TRIMS over-extended / corny endings.

Kept deliberately SEPARATE from generation: piling more "don't" rules into the generator
degrades its output top-down. Instead we generate freely, then this layer cuts the tacked-on
tail back to the version that hits. It can ONLY trim — never rewrite or add — so it can't hurt
a caption, only tighten it.
"""
from __future__ import annotations

import json

from anthropic import Anthropic

from app.config import settings

_SYS = """You are a ruthless editor for ONE creator's blunt, cocky, money-alpha captions.

THE RECURRING PROBLEM you fix: a caption has a STRONG blunt core, then RUINS it with an over-extended, corny, sentimental, or motivational-speaker ENDING. Your ONLY job is to CUT each caption back to the version that hits hardest — usually the first one or two sentences / the blunt core. Less is more.

CUT: stretched metaphors taken a beat too far, tacked-on second/third payoffs, "go build / go earn / stop trying" motivational closers, soft / wistful / poetic tails, anything that could be read aloud in a tender voice.
KEEP: the blunt core and blunt insult tags ("soft ahh", "broke ahh", "pussy"). Do NOT rewrite, reword, or ADD anything — ONLY trim. If a caption is already tight and hits, return it UNCHANGED.

Examples (input -> trimmed):
- "broke people save for a rainy day. i bought the cloud. now it only rains on the ones who didn't, and i pick the forecast." -> "broke people save for a rainy day. i bought the cloud."
- "a steady paycheck is the most expensive thing you'll ever buy. you pay for it with every idea you were too scared to test. the bill doesn't come due till 65, and by then it's the whole life." -> "a steady paycheck is the most expensive thing you'll ever buy. you pay for it with every idea you were too scared to test."
- "you don't have a spending problem, you got a coward's income. nobody ever subtracted their way to rich — you can budget a hole spotless and it's still a hole. put the spreadsheet down and go earn, soft ahh." -> "you don't have a spending problem, you got a coward's income. put the spreadsheet down and go earn, soft ahh."
- "every broke guy's waiting on the one bet that fixes everything. the casino's never spun a wheel in its life and it's the richest thing in the building. stop trying to win the game. go build one that takes a cut either way." -> "every broke guy's waiting on the one bet that fixes everything. the casino's never spun a wheel in its life and it's the richest thing in the building."

Return ONLY JSON, same count and order as the input, \\n preserved for line breaks:
{"edited": ["trimmed caption 1", "trimmed caption 2"]}"""


def refine(candidates: list[dict]) -> list[dict]:
    """Trim over-extended / corny tails from each candidate. Falls back to originals on any error."""
    texts = [c.get("text", "") for c in candidates]
    if not texts:
        return candidates
    user = "Trim these (return the SAME count and order):\n" + json.dumps(texts, ensure_ascii=False)
    try:
        msg = Anthropic(api_key=settings.anthropic_api_key, max_retries=4).messages.create(
            model=settings.caption_model,
            max_tokens=3000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=_SYS,
            messages=[{"role": "user", "content": user}],
        )
        out = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        start, end = out.find("{"), out.rfind("}")
        edited = json.loads(out[start : end + 1]).get("edited", [])
    except Exception:  # noqa: BLE001 — editor must never break generation
        return candidates
    if len(edited) != len(candidates):
        return candidates  # count mismatch -> keep originals (safety)
    result = []
    for c, t in zip(candidates, edited):
        c = dict(c)
        if isinstance(t, str) and t.strip():
            c["text"] = t.strip()
        result.append(c)
    return result
