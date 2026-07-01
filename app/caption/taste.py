"""Taste learning for SELECTION — turn reel-grade notes into pairwise preferences + calibrate the chooser.

The operator's notes on a graded reel frequently name a BETTER caption that was already in the candidate
set ("[that alt] would have been an 8"). That is a pairwise preference (better > posted) sitting right in
the data. `learn_from_reel` extracts it (per graded reel) into the existing per-profile pairwise store;
`calibration` feeds the freshest of those corrections + the highest-rated reels back into the chooser, so
selection learns what THIS operator actually posts instead of a generic "which is sharpest" gut.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json
from app.corpus import grades as grade_store
from app.corpus import reels as reel_store

_MATCH_SYS = """You read an operator's NOTE on a short-form reel they graded, the caption that was POSTED, and the OTHER candidate captions that were available. Decide whether the note indicates a SPECIFIC other candidate would have been BETTER than the posted one — operators usually quote it and say things like "would have been an 8" or "would have worked better". If the note only critiques the posted caption without preferring a specific listed alternative, return null.

Return ONLY JSON: {"better_index": <0-based index into the CANDIDATES list of the better candidate, or null>}"""


def learn_from_reel(record: dict) -> bool:
    """If the note names a better non-chosen candidate, record it as a pairwise preference (better > posted,
    per active profile). Idempotent (record_pairwise dedups). Returns True if a pair was captured."""
    note = ((record.get("grade") or {}).get("notes") or "").strip()
    cands = record.get("candidates") or []
    posted_i = next((i for i, c in enumerate(cands) if c.get("chosen")), None)
    if not note or len(cands) < 2 or posted_i is None:
        return False
    listing = "\n".join(f"[{i}] {(c.get('text') or '').strip()}" for i, c in enumerate(cands))
    user = f"POSTED index: {posted_i}\n\nCANDIDATES:\n{listing}\n\nNOTE: {note}"
    try:
        out = complete_json(_MATCH_SYS, user, effort="low", max_tokens=200)
        s, e = out.find("{"), out.rfind("}")
        bi = json.loads(out[s:e + 1]).get("better_index") if s != -1 else None
    except Exception:  # noqa: BLE001
        return False
    if isinstance(bi, int) and 0 <= bi < len(cands) and bi != posted_i:
        winner = (cands[bi].get("text") or "").strip()
        loser = (cands[posted_i].get("text") or "").strip()
        if winner and loser and winner != loser:
            grade_store.record_pairwise(winner, loser, {"source": "reel_note"})
            return True
    return False


def calibration(n_pairs: int = 5, n_best: int = 4) -> str:
    """A compact 'this is what THIS operator actually posts' block for the chooser: the freshest corrections
    (you'd have posted X, not Y) + the highest-rated reels (your bar). Empty until there is graded data."""
    pairs = [g for g in grade_store.load_grades() if g.get("type") == "pairwise"][-n_pairs:]
    bests = [(r.get("caption"), (r.get("grade") or {}).get("rating"))
             for r in reel_store.graded() if ((r.get("grade") or {}).get("rating") or 0) >= 8][:n_best]
    parts: list[str] = []
    if pairs:
        parts.append("Picks you'd correct — you would have posted the FIRST, not the second:")
        for g in pairs:
            parts.append(f"  YOU'D POST: {(g.get('winner') or '').strip()[:140]}")
            parts.append(f"  NOT:        {(g.get('loser') or '').strip()[:140]}")
    if bests:
        parts.append("Reels you rated highly (this is your bar):")
        for cap, r in bests:
            parts.append(f"  [{r}/10] {(cap or '').strip()[:140]}")
    return "\n".join(parts)
