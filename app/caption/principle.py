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
# MOVES are MECHANISMS, not templates. Each names HOW the twist works — never a fixed sentence shape.
# The phrasings in parentheses are ONE example each; the engine must invent a fresh STRUCTURE every time
# (the failure mode is a move collapsing to one signature scaffold that repeats across batches).
MOVES = [
    ("iykyk_decode", "A clean, wholesome-sounding surface that DECODES to something filthy/dark/unhinged — the gap is the joke. Vary the reveal hard: NEVER default to 'Meant I…'. Sometimes the decode is one swapped word, sometimes the reader just realizes it, sometimes it's buried mid-sentence."),
    ("precise_equivalence", "Two things that map EXACTLY (both dressing a deficit as an asset, or secretly identical). The precision is the wit. Vary the frame: do NOT always open 'X is like / the same as Y' — sometimes lead with the punchline, or a question, or state it flat."),
    ("possessive_escalation", "Jealousy escalated one absurd step PAST reason, deadpan — and it must read ABSURD-funny, never genuinely insecure/controlling (Check is unbothered). Vary the shape; do NOT default to 'you let your girl X? mine Y'. Use sparingly (~1 per batch max)."),
    ("hater_callout", "Predict the EXACT pathetic cope a hater uses to downplay a real win — confident and unbothered, never bitter. Vary the framing: do NOT always open 'ninjas hate so much you could ___'; sometimes lead with the cope, sometimes a scene. Don't lean on a car/money win every time."),
    ("anti_simp_redirect", "Set up 'a real man does X for her' then redirect the payoff to someone else. Find FRESH redirects — do NOT keep landing on 'the next guy inherits the finished product / Carfax'."),
    ("anti_mediocrity_dread", "A sharp, true jab at settling that lands as DREAD — comfort made scary (the eulogy, the unchanged group chat, the retirement body). Motivation is IMPLIED by the dread; never a 'you should'."),
    ("deadpan_villain", "A flatly-stated, indefensible villain take delivered like it's obviously correct — zero remorse, zero self-pity, unbothered and dark-funny. Code the villainy on loyalty / effort / attention / time — NOT money or legacy (inheritance, new car, come-up); avoid any new-money / poster edge."),
    ("antideep_parody", "MOCK fake-deep guru/poster energy: quote a proverb, then undercut it with a funny-petty or absurd reality. NEVER a sad/down-bad reality (Check never spirals). VARY the undercut HARD — do NOT always land on 'refreshing a story / view count / follower count'. Rotate the very-online compulsion: doomscrolling, re-reading his own sent text, screenshotting to the gc, leaving then deleting a comment, watching his own story to see who's at the top — and change the proverb every time. The point is to make fun of posters, not be one."),
    ("absurd_image_motivating", "An absurd/degenerate IMAGE (an animal, a lowlife, a scene) that IMPLIES resilience or a truth — then STOP. NO 'be the X bro', NO 'you should', NO direct-address sermon. The image is the entire bit; the motivation is felt, never stated."),
    ("relatable_self_own", "A relatable observation that flips into a self-own or a brutal truth about the bit everyone secretly does — self-aware but still cool, never down-bad."),
    ("specific_bro_truth", "A deadpan, HYPER-SPECIFIC truth about the boys / loyalty / group-chat life / dating dynamics (never a platitude) — the kind the whole group chat quote-tweets. Favor BEHAVIORAL tells (the 'knew bro got dumped — the typing dots came back after months' kind), NOT read-receipt / location / view-count surveillance (overused). Vary the structure each time. UNDERUSED; reach for it often."),
    ("unbothered_standards", "An effortless status/taste/standards flex that is NEVER about money or a job — what he won't tolerate, the company he keeps, what he doesn't chase or explain, the calm of not caring. Cool, not try-hard."),
]

# Force breadth — the failure mode is ~60% girls/dating. Spread premises across these; girls capped low.
SUBJECTS = [
    "your boys / loyalty / the group chat", "status / confidence / standards / taste",
    "ambition vs settling / mediocrity", "online life / clout / the timeline",
    "haters / doubters", "family / where you came from", "deadpan everyday-life observations",
    "girls / dating (AT MOST a third of the batch — do not let this dominate)",
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
        + "Your MOVES — the MECHANISMS behind your best captions (a toolkit, NOT templates; each can take any shape):\n"
        + _moves_block(moves)
        + "\n\nSUBJECTS to spread across (mandatory — do NOT let girls/dating dominate):\n"
        + "\n".join(f"- {s}" for s in SUBJECTS)
        + f"\n\nBrainstorm {k} fresh caption PREMISES for new posts. Rules:\n"
        "- Each premise = a DIFFERENT move on a DIFFERENT subject. Spread WIDE across BOTH — AT MOST a third about girls/dating.\n"
        "- AT MOST ONE premise may resolve on a read-receipt / 'on read' / view-count / story-view beat — that anxiety is overused; reach for other behaviors and tells.\n"
        "- A premise is a one-line seed (which move + subject + the specific angle), NOT a finished caption.\n"
        "- Reach for ABSOLUTE BANGERS — surprising, true, sharp, screenshot-worthy.\n"
        "- Pick angles that will force DIFFERENT sentence shapes — not eight variations of one structure.\n\n"
        f"Don't echo these recent lines, and don't reuse their STRUCTURES:\n{avoid}\n\n"
        'ONLY JSON: {"premises": [{"move": "<move>", "subject": "<which subject>", "angle": "<the specific fresh angle>"}]}'
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
            f"Premise — move: {p.get('move', '?')} | subject: {p.get('subject', '?')} | angle: {p.get('angle')}\n\n"
            "Write the SINGLE sharpest caption that executes THIS move on THIS angle, unmistakably in your voice. "
            "Nail the twist precisely, deadpan, hyper-specific.\n"
            "- INVENT a fresh STRUCTURE. Do NOT use a stock scaffold ('you let your girl…', 'ninjas hate so much you "
            "could…', 'X is like Y', 'Meant I…', 'be the ___ bro'). Vary the sentence shape so no two captions feel like the same joke.\n"
            "- If it's motivating, IMPLY it — never a 'you should' / direct-address sermon.\n"
            "- Never broke, never business/job/client cosplay, never insecure or down-bad.\n\n"
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
