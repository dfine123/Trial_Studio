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

_MATCH_SYS = """You read an operator's NOTE on a short-form reel they graded, the caption that was POSTED, and the OTHER candidate captions that were available. Answer two things:
1) better_index: does the note indicate a SPECIFIC other LISTED candidate would have been BETTER than the posted one? (Operators quote it and say "would have been an 8" / "would have worked better".) Give its 0-based index into CANDIDATES, or null if the note doesn't clearly prefer a specific listed alternative.
2) off_voice: is the note saying the POSTED caption is OFF this creator's VOICE / STANCE — earnest, self-pitying, "emo", corny, an earnest-corporate or grind read, "not aligned with the high level voice"? This is specifically about voice/STANCE, NOT about a line merely being weak or forced. true or false.

Return ONLY JSON: {"better_index": <index or null>, "off_voice": <true|false>}"""


def learn_from_reel(record: dict) -> dict:
    """Mine a graded reel's note (per active profile, idempotent). Captures (a) a pairwise preference if it
    names a better listed candidate, and (b) an off_voice STANCE negative on the posted caption if the note
    flags the voice/stance as wrong. Returns {"pairwise": bool, "off_voice": bool}."""
    got = {"pairwise": False, "off_voice": False}
    note = ((record.get("grade") or {}).get("notes") or "").strip()
    cands = record.get("candidates") or []
    posted_i = next((i for i, c in enumerate(cands) if c.get("chosen")), None)
    if not note or posted_i is None:
        return got
    listing = "\n".join(f"[{i}] {(c.get('text') or '').strip()}" for i, c in enumerate(cands))
    user = f"POSTED index: {posted_i}\n\nCANDIDATES:\n{listing}\n\nNOTE: {note}"
    try:
        out = complete_json(_MATCH_SYS, user, effort="low", max_tokens=200)
        s, e = out.find("{"), out.rfind("}")
        d = json.loads(out[s:e + 1]) if s != -1 else {}
    except Exception:  # noqa: BLE001
        return got
    bi = d.get("better_index")
    if isinstance(bi, int) and 0 <= bi < len(cands) and bi != posted_i:
        winner, loser = (cands[bi].get("text") or "").strip(), (cands[posted_i].get("text") or "").strip()
        if winner and loser and winner != loser:
            grade_store.record_pairwise(winner, loser, {"source": "reel_note"})
            got["pairwise"] = True
    if d.get("off_voice") is True:
        posted = (cands[posted_i].get("text") or "").strip()
        if posted:
            grade_store.record_verdict(posted, "off_voice", {"source": "reel_note"})
            got["off_voice"] = True
    return got


def stance_block(n: int = 4) -> str:
    """ON-VOICE / OFF-VOICE calibration for the system prompt, learned from graded reels + off_voice notes.
    Same energy, right vs wrong STANCE — SHOWN, not told. Empty until there is graded data."""
    offs = grade_store.off_voice_captions()[-n:]
    ons = [r.get("caption") for r in reel_store.graded()
           if ((r.get("grade") or {}).get("rating") or 0) >= 8][:n]
    if not offs and not ons:
        return ""
    parts = ["Calibration from reels you've graded — same energy, RIGHT vs WRONG voice/stance:"]
    for c in ons:
        parts.append(f"  ON-VOICE:  {(c or '').strip()[:130]}")
    for c in offs:
        parts.append(f"  OFF-VOICE (fine line, wrong stance — earnest/self-pity/corny): {(c or '').strip()[:130]}")
    return "\n".join(parts)


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
