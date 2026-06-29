"""Principle-driven generation (v2) — the formula rebuild.

v1 anchored each caption to ONE reference and reproduced its exact format. That's rigid (forces a
format onto a subject it doesn't fit), repetitive at scale (cycles the same refs), and "ripped off"
(clones a ref). v2 generates from the MOVES — the comedic mechanisms behind the voice — and INVENTS:

  IDEATE  -> brainstorm K diverse premises, each a DIFFERENT move on a fresh subject (this is where
             range + creativity live; the moves are a toolkit, not templates).
  CRAFT   -> write the single sharpest line executing each premise, in voice (this is where the
             twist/precision/voice live). Invent the wording; never clone an example.

The persona + a corpus sample calibrate the VOICE and the bar (shared format base via voice_system);
the references are never reproduced. Moves are the unit the grading loop will reinforce/kill/expand,
so winning FORMATS (e.g. the hater-callout) survive even if a specific reference doesn't.
"""
from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor

from app.caption.engine import voice_system
from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs

# The MOVES — the mechanisms distilled from the corpus (the principles, not templates). Voice-neutral:
# the persona supplies the voice, the move supplies the structure of the twist.
MOVES = [
    ("iykyk_decode", "A line that reads clean/innocent but DECODES to something filthy, dark, or unhinged — the gap between the surface read and the real meaning IS the joke."),
    ("precise_equivalence", "'X is like Y' where the two map EXACTLY (usually both dressing a deficit up as an asset, or both secretly the same thing). The wit is the precision of the mapping, never the shape."),
    ("possessive_escalation", "A deadpan jealous/possessive take escalated one absurd step too far, delivered like it's totally reasonable ('you let your girl ___, I don't even like mine ___')."),
    ("hater_callout", "Call out the haters/doubters by predicting the EXACT pathetic way they'd downplay a win ('🥷s hate so much you could ___ and they'd still say ___'). Their cope is the punchline."),
    ("anti_simp_redirect", "Set up a 'good man does X for her' line, then redirect the credit/payoff to someone else (her ex, the other guy) so the simp ends up with nothing."),
    ("small_op_flex", "Pretend to be a bigger operation than you are, then reveal it's just you and bro ('let me loop in the team' — it's one guy on a call)."),
    ("anti_mediocrity_dread", "A sharp jab at settling/mediocrity that lands as DREAD, not a poster — someone could step into your life and do it better; the 9-5 framed like a horror movie. Motivating by making comfort scary."),
    ("deadpan_villain", "A flatly-stated, indefensible villain take delivered like it's obviously correct, no remorse ('raise her rent', 'he wasn't there when I was down')."),
    ("antideep_parody", "Parody fake-deep guru/proverb energy — either take a 'profound' line somewhere stupid, or state a deepity that's actually nonsense ('I'd rather be broke and rich than sad and happy')."),
    ("crude_to_motivating", "Open with a crude/degenerate analogy and TWIST it into something genuinely (or absurdly) motivating ('be more like a crackhead — they get up and make it happen no matter what')."),
    ("relatable_self_own", "A relatable observation that flips into a self-own or a brutal truth about the bit everyone secretly does ('idk where i'd be without her — prob further in life')."),
    ("specific_bro_truth", "A deadpan, HYPER-SPECIFIC truth (never a platitude) that hits because it's exactly right — sincere but sharp, the opposite of a motivational poster ('the cold water doesn't get warmer the longer you wait')."),
]


def _moves_block(moves) -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in moves)


def generate_v2(k: int = 8, notes: str | None = None) -> list[dict]:
    """Principle-driven generation. Returns [{text, move}] (move kept for grade attribution)."""
    refs = [(r.get("caption") or "").strip() for r in load_refs() if (r.get("caption") or "").strip()]
    random.shuffle(refs)
    sys = voice_system("\n\n".join(refs[:24]))                 # persona + voice/bar + shared mechanics
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(60)) or "(none yet)"
    note = (notes or "").strip()
    moves = list(MOVES)
    random.shuffle(moves)

    # STAGE 1 — IDEATE: K diverse premises, each a different move on a fresh subject (range + creativity)
    ideate_user = (
        (f"Lean (soft, optional): {note}\n\n" if note else "")
        + "Here are your MOVES — the mechanisms behind your best captions. They're a TOOLKIT, not templates:\n"
        + _moves_block(moves)
        + f"\n\nBrainstorm {k} fresh caption PREMISES for new posts. Each premise applies a DIFFERENT move to a "
        "fresh, specific subject from your world (girls, your boys, dating, loyalty, status, the come-up, online "
        "life, work, family — whatever's real to you). Spread WIDE across both moves and subjects — no two alike. "
        "A premise is a one-line seed: which move + the specific angle. NOT a finished caption. Reach for angles "
        "that could become ABSOLUTE BANGERS — surprising, true, sharp; the kind that get screenshotted.\n\n"
        f"Don't echo these recent ones:\n{avoid}\n\n"
        'ONLY JSON: {"premises": [{"move": "<move name>", "angle": "<the specific fresh angle>"}]}'
    )
    out = complete_json(sys, ideate_user, effort="high", max_tokens=2000)
    s, e = out.find("{"), out.rfind("}")
    if s == -1:
        return []
    try:
        premises = json.loads(out[s:e + 1]).get("premises", [])[:k]
    except json.JSONDecodeError:
        return []
    premises = [p for p in premises if isinstance(p, dict) and (p.get("angle") or "").strip()]
    if not premises:
        return []

    # STAGE 2 — CRAFT: invent the sharpest line for each premise, in voice (twist + precision + voice)
    def craft(p: dict) -> dict | None:
        user = (
            f"Premise to execute — move: {p.get('move', '?')} | angle: {p.get('angle')}\n\n"
            "Write the SINGLE sharpest caption that executes THIS move on THIS angle, unmistakably in your "
            "voice. Nail the twist precisely, deadpan, hyper-specific. INVENT the exact line from scratch — do "
            "NOT reuse or rephrase any example you've seen; this has to feel new. Make it land as a banger.\n\n"
            'ONLY JSON: {"text": "the caption (\\n for line breaks)"}'
        )
        t = complete_json(sys, user, effort="high", max_tokens=900)
        a, b = t.find("{"), t.rfind("}")
        if a == -1:
            return None
        try:
            cap = (json.loads(t[a:b + 1]).get("text") or "").strip()
        except json.JSONDecodeError:
            return None
        return {"text": cap, "move": p.get("move")} if cap else None

    with ThreadPoolExecutor(max_workers=min(8, len(premises))) as ex:
        crafted = [c for c in ex.map(craft, premises) if c]

    refined = refine(crafted)                                  # subtractive cleanup (preserves move)
    out_list = []
    for orig, rf in zip(crafted, refined):
        txt = (rf.get("text") or "").strip()
        if txt:
            out_list.append({"text": txt, "move": orig.get("move")})
    log_generated([c["text"] for c in out_list])
    return out_list
