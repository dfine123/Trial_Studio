"""Refinement layer — a ruthless editor pass that cleans each caption: trims over-extended /
corny ENDINGS and strips corny performative pet-names ("ma'am", "baby", ...).

Kept deliberately SEPARATE from generation: piling more "don't" rules into the generator
degrades its output top-down. Instead we generate freely, then this layer SUBTRACTS the cringe.
It can ONLY trim or strip — never rewrite or add — so it can't hurt a caption, only tighten it.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You are a ruthless editor for ONE creator's blunt, very-online captions. You ONLY ever SUBTRACT — trim or strip — you NEVER rewrite, reword, or add. Two jobs:

1) TRIM over-extended / corny ENDINGS back to the blunt core. CUT: stretched metaphors taken a beat too far, tacked-on second/third payoffs, "go build / go earn / stop trying" motivational closers, soft / wistful / poetic tails — anything that could be read aloud in a tender voice.

2) STRIP corny performative PET-NAME address — "ma'am", "baby", "babe", "sweetheart", "darling", "sweetie", "honey", "champ", "sport", "kiddo" — when used to address the target (the usual case). Delete the pet-name AND any "relax" / "aww" / "nah" lead-in that exists only to set up that address. It reads corny/performative and cheapens the line. (Only keep one in the rare case it's genuinely load-bearing to the joke.)

KEEP everything else exactly — the blunt core, the slang, the blunt insult tags ("soft ahh", "broke ahh", "pussy"). If a caption is already tight and clean, return it UNCHANGED.

Examples (input -> edited):
- "broke people save for a rainy day. i bought the cloud. now it only rains on the ones who didn't, and i pick the forecast." -> "broke people save for a rainy day. i bought the cloud."
- "every broke guy's waiting on the one bet that fixes everything. the casino's never spun a wheel in its life and it's the richest thing in the building. stop trying to win the game. go build one." -> "every broke guy's waiting on the one bet that fixes everything. the casino's never spun a wheel in its life and it's the richest thing in the building."
- "she said her ex was emotionally unavailable. ma'am my emotions been in a margin call since 2021" -> "she said her ex was emotionally unavailable. my emotions been in a margin call since 2021"
- "relax ma'am i don't date charity cases either" -> "i don't date charity cases either"
- "girl said she wants loyalty. baby i can't even commit to one income stream." -> "girl said she wants loyalty. i can't even commit to one income stream."

Return ONLY JSON, same count and order as the input, \\n preserved for line breaks:
{"edited": ["edited caption 1", "edited caption 2"]}"""


def refine(candidates: list[dict]) -> list[dict]:
    """Trim over-extended/corny tails AND strip corny pet-name address (subtractive only). Falls back to originals on error."""
    texts = [c.get("text", "") for c in candidates]
    if not texts:
        return candidates
    user = "Edit these (trim corny tails + strip pet-names; SAME count and order):\n" + json.dumps(texts, ensure_ascii=False)
    try:
        out = complete_json(_SYS, user, effort="medium", max_tokens=3000)
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
