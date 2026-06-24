"""Seeding experiment — random-word divergence seeding (isolated from the production engine).

Concept (user's): feed ONE random word per caption as a loose seed. The word need NOT appear in,
relate to, or apply to the caption — it's pure perturbation to make the voice variate NATURALLY
instead of locking into modes. Works off ONE collective mode (the full voice), not per-trait.

Contrast with engine.py's spark-pairing (which anchors each slot to a real reference STRUCTURE, so
output rides close to the references). A random word anchors to NOTHING, so output should be fresher.
Graded in its own isolated store (grades_seed.jsonl) so it never contaminates the real signal.
"""
from __future__ import annotations

import json
import random

from app.caption.engine import _SYS  # reuse the exact voice + DNA block (one collective mode)
from app.caption.llm import complete_json
from app.caption.randword import random_words
from app.caption.refine import refine
from app.corpus.genlog import recent_generated
from app.corpus.store import load_refs


def generate_seeded(n: int = 8, notes: str | None = None) -> list[dict]:
    """Full-voice generation with one random-word divergence seed per slot. Each candidate carries
    its `seed` word so the grade page can show it. No spark-pairing, no per-trait modes."""
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    seeds = random_words(n)
    seed_block = "\n".join(f"SEED {i + 1}: {w}" for i, w in enumerate(seeds))
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "For each caption you write, I'm handing you ONE random seed word. Let it nudge you "
        "somewhere you wouldn't have gone on your own — it might suggest a subject, an image, a "
        "shape, an angle, or a feeling. It does NOT need to appear in the caption, be referenced, "
        "or even relate to it at all — in plenty of cases the finished caption will have nothing to "
        "do with the word, and that's completely fine. It's purely a spark to keep you out of the "
        "same few lanes. Your VOICE doesn't change at all — the seed only shifts WHERE you point it.\n\n"
        + seed_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per seed, in order. ONLY JSON, no prose: "
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
            out.append(c)
    return refine(out)  # refine preserves the `seed` key (dict(c)) + count/order
