"""Taste learning for SELECTION — turn the operator's grades into a high-level UNDERSTANDING of what makes
this creator's captions land, and feed it to the chooser.

The core is `refresh_taste`/`distilled_taste`: an LLM reads everything the operator has graded — the ones
they rated highly (these HIT) and the low ones with their notes (these MISSED, and why) — and distills a
compact, high-level read of what SEPARATES the hits from the misses for THIS creator (execution/landing
qualities, format-agnostic — the format range is sacred and must never be narrowed). The chooser reads
that taste to pick the best WHOLE caption. `learn_from_reel` still mines each note into pairwise/off_voice
signals (kept as data + history); `calibration` is the older raw-example block, superseded by the distilled
taste for the chooser.
"""
from __future__ import annotations

import json
import os

from app import profiles
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


# ---- Distilled TASTE: the high-level understanding of what makes THIS creator's captions land ----

_DISTILL_SYS = """You are studying ONE creator's graded short-form captions to distill their TASTE — a
high-level understanding of what makes THEIR captions actually LAND versus fall flat. You are given the
ones they rated highly (these HIT) and the ones they rated low with their own notes (these MISSED, and why).

Write a tight, high-level read of what SEPARATES the hits from the misses for THIS creator: the qualities
that make one of their lines genuinely connect and land, and the failure patterns that make one fall flat.
Ground it in the examples, but state it as UNDERSTANDING a reader can use to judge a NEW caption — not a
checklist of "do X / don't Y" rules.

CRITICAL: this is about EXECUTION and what LANDS — NOT about which topics or FORMATS to use. This creator
deliberately posts across a WIDE range of formats (crude wordplay, villain flex, degenerate confession,
absurd bits, self-owns, sincere grindset wisdom, and more) and that RANGE IS SACRED — never suggest
narrowing it, never favor some formats over others, never imply a topic is off-limits. Capture only the
delivery/landing qualities that hold ACROSS all of them.

~6-9 sentences, concrete, plain language. Return ONLY JSON: {"taste": "<the understanding>"}"""


def _taste_path(pid=None) -> str:
    return profiles.taste_path(pid)   # suffixed per active test backend (isolated); shared voice stays shared


def _rating(r: dict) -> int:
    return (r.get("grade") or {}).get("rating") or 0


def refresh_taste(pid=None, min_grades: int = 8) -> dict:
    """(Re)build the distilled TASTE from everything graded so far — a high-level, format-agnostic read of
    what makes this creator's captions land vs miss. Cached per-profile for the chooser to read. Best-effort:
    returns {ok:false, reason} rather than raising."""
    reels = reel_store.graded(pid)
    if len(reels) < min_grades:
        return {"ok": False, "reason": f"only {len(reels)} graded reels (need >= {min_grades})"}
    hits = [r for r in reels if _rating(r) >= 8]
    misses = [r for r in reels if 0 < _rating(r) <= 4]

    def fmt(rs: list[dict]) -> str:
        lines = []
        for r in rs[:45]:
            c = (r.get("caption") or "").replace("\n", " / ").strip()[:180]
            n = ((r.get("grade") or {}).get("notes") or "").strip()[:180]
            lines.append(f"- {c}" + (f"   [why: {n}]" if n else ""))
        return "\n".join(lines) or "(none yet)"

    user = (f"CAPTIONS THAT HIT (rated 8-10):\n{fmt(hits)}\n\n"
            f"CAPTIONS THAT MISSED (rated 1-4), with the creator's own notes:\n{fmt(misses)}")
    try:
        out = complete_json(_DISTILL_SYS, user, effort="high", max_tokens=1100)
        s, e = out.find("{"), out.rfind("}")
        taste = (json.loads(out[s:e + 1]).get("taste") or "").strip() if s != -1 else ""
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "reason": f"distill failed: {ex}"}
    if not taste:
        return {"ok": False, "reason": "empty taste"}
    p = _taste_path(pid)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(taste)
    os.replace(tmp, p)
    return {"ok": True, "grades": len(reels), "hits": len(hits), "misses": len(misses), "taste": taste}


def distilled_taste(pid=None) -> str:
    """The cached high-level taste (what makes this creator's captions hit/miss), for the chooser.
    Empty until refresh_taste has run with enough graded data."""
    try:
        with open(_taste_path(pid), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ""
