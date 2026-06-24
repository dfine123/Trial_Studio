"""Seeding experiment — random-word DIVERGENCE seeding (isolated test env).

User's concept ("seeding"): a random word per caption that only LOOSELY directs it — the word need
not appear in / relate to / apply to the output; it can have "absolutely nothing to do with the
caption output." The word should "extrapolate to an ALIGNING STRUCTURE that is completely different
than the actual caption meaning or theme."

v1 MISTAKE: pairing each slot to "SEED: <word>" made the model write ABOUT the word every time — the
opposite of the concept. v2 (faithful to "aligning structure, completely different meaning"): TWO
stages. (1) abstract each random word into a transferable STRUCTURAL PATTERN, deliberately discarding
its literal topic (avalanche -> "something tiny triggers a massive total collapse"). (2) generation
only ever sees the PATTERNS, never the words — so it writes a caption that embodies the shape about
something totally unrelated, and the word literally cannot appear. Off ONE collective mode (full
voice). Each candidate carries its seed word + derived structure for the grade page.
"""
from __future__ import annotations

import json
import random

from app.caption.engine import _SYS
from app.caption.llm import complete_json
from app.caption.randword import random_words
from app.caption.refine import refine
from app.corpus.genlog import recent_generated
from app.corpus.store import load_refs

_ABSTRACT_SYS = """You turn a random word into an ABSTRACT STRUCTURAL PATTERN — the underlying shape, dynamic, or relationship it suggests — and THROW AWAY the word's literal topic. Never describe the thing itself; name the transferable pattern in one short line that could apply to a hundred unrelated subjects.

Examples:
- avalanche -> "something tiny sets off a massive, total collapse all at once"
- lighthouse -> "a fixed warning everyone can see and everyone ignores until it's too late"
- recipe -> "exact steps followed perfectly that still produce a different result every time"
- mirror -> "it only ever shows you what you already brought to it"
- ladder -> "every step up quietly removes the one you were just standing on"
- thermostat -> "one tiny thing nobody notices silently deciding the comfort of the whole room"

Return ONLY JSON, same count and order as the input: {"patterns": ["pattern 1", "pattern 2", ...]}"""


def _abstract_structures(words: list[str]) -> list[str]:
    """Stage 1: random word -> transferable structural pattern (literal topic discarded). Falls back
    to the raw words on any failure so the batch still generates."""
    if not words:
        return []
    user = "Turn each into a transferable structural pattern (drop the literal topic):\n" + json.dumps(words, ensure_ascii=False)
    try:
        out = complete_json(_ABSTRACT_SYS, user, effort="medium", max_tokens=1500)
        start, end = out.find("{"), out.rfind("}")
        patterns = json.loads(out[start:end + 1]).get("patterns", [])
    except Exception:  # noqa: BLE001
        return list(words)
    if len(patterns) != len(words):
        return list(words)
    return [p if isinstance(p, str) and p.strip() else w for p, w in zip(patterns, words)]


def generate_seeded(n: int = 8, notes: str | None = None) -> list[dict]:
    """Two-stage divergence seeding. Generation sees only abstract STRUCTURES (never the words), so a
    caption embodies a shape about unrelated content and the seed word cannot leak in."""
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    seeds = random_words(n)
    structures = _abstract_structures(seeds)
    struct_block = "\n".join(f"STRUCTURE {i + 1}: {p}" for i, p in enumerate(structures))
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Each caption tonight is sparked by an abstract STRUCTURE — a shape/dynamic, not a subject. "
        "For each one, write a caption that quietly EMBODIES that shape but about something completely "
        "unrelated and your own — the structure is a hidden skeleton, NOT the topic. Never describe the "
        "structure literally; a reader should just feel a sharp caption, never guess the scaffold:\n\n"
        + struct_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per structure, in order. ONLY JSON, no prose: "
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
    out = []
    for i, c in enumerate(cands[:n]):
        if isinstance(c, dict) and (c.get("text") or "").strip():
            c["seed"] = seeds[i] if i < len(seeds) else None
            c["structure"] = structures[i] if i < len(structures) else None
            out.append(c)
    return refine(out)  # refine preserves seed + structure (dict(c)) + count/order
