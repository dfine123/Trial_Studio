"""Living corpus — promote operator-validated bangers (graded 9-10 reels) into the reference corpus.

This is the engine's primary learning loop: grades -> GROUNDING. A 9-10 line the operator rated on a real
reel is exactly the voice at its best, so it joins references.jsonl as a first-class ref (rotates as an
anchor, shows in the voice block) with a why_it_works that decodes the EXECUTION principles — what made
this rendition land — so the system learns why things work, expanding range rather than limiting it.
Operator-gated: nothing promotes without an explicit click. Provenance-tagged (source=promoted_gen,
promoted_from=anchor lineage, ref_id p###) and deduped against the existing corpus.
"""
from __future__ import annotations

import json
import os
import re

from app import profiles
from app.caption.llm import complete_json
from app.corpus import reels as reel_store
from app.corpus.store import load_refs

_LABEL_SYS = """You are annotating ONE caption for a creator's reference corpus — it was operator-rated 9-10/10 on a real post, so it IS the voice at its best. Decode WHY IT WORKS at the EXECUTION level: the actual mechanism of the joke/insight AND what makes this exact rendition land (the precise word/image/logic/rhythm that snaps) — nuanced and faithful to THIS line, never generic advice. Also give a precise persona_trait (open vocabulary, e.g. shameless_villain, self_aware_hustler, deadpan_crude, absurd_motivational, deep_bro_sincere, anticope_callout) and a primary_lever (e.g. shareability, comment_bait, iykyk_decode, relatability).

Return ONLY JSON: {"why_it_works": "...", "persona_trait": "...", "primary_lever": "..."}"""


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _next_ref_id(refs: list[dict]) -> str:
    mx = 0
    for r in refs:
        m = re.fullmatch(r"p(\d+)", r.get("ref_id") or "")
        if m:
            mx = max(mx, int(m.group(1)))
    return f"p{mx + 1:03d}"


def promotable(pid=None, min_rating: int = 9) -> list[dict]:
    """Graded reels rated >= min_rating whose caption isn't in the corpus yet (newest first)."""
    existing = {_norm(r.get("caption") or "") for r in load_refs(profiles.corpus_path(pid))}
    out = []
    for r in reel_store.graded(pid):
        rating = (r.get("grade") or {}).get("rating") or 0
        cap = (r.get("caption") or "").strip()
        if rating >= min_rating and cap and _norm(cap) not in existing and not r.get("promoted"):
            out.append({"reel_id": r.get("reel_id"), "caption": cap, "rating": rating,
                        "notes": (r.get("grade") or {}).get("notes"),
                        "anchor_refs": r.get("caption_anchor_refs") or []})
    return out


def _too_similar(a: str, b: str, thr: float = 0.8) -> bool:
    """Near-duplicate check: word-set containment. Catches the same joke re-rendered ("doesn't" vs
    "don't") without flagging genuinely different lines that merely share a topic."""
    wa, wb = set(_norm(a).split()), set(_norm(b).split())
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= thr


def _add_ref(caption: str, rating: int, anchors: list, source: str, note: str, pid=None) -> str | None:
    """Append one operator-validated caption to the corpus (deduped, incl. NEAR-duplicates — a format
    must never stack multiple copies of the same joke, or it gets double the rotation slots + double
    the voice-block priming). Returns the new ref_id or None."""
    cap = (caption or "").strip()
    if not cap:
        return None
    refs = load_refs(profiles.corpus_path(pid))
    if _norm(cap) in {_norm(r.get("caption") or "") for r in refs}:
        return None
    if any(_too_similar(cap, r.get("caption") or "") for r in refs):
        return None   # same joke, different rendition — one copy is enough
    try:    # decode the execution principles (why THIS rendition lands) — the learning content
        out = complete_json(_LABEL_SYS, f"CAPTION:\n{cap}", effort="high", max_tokens=600, tag="promote-label")
        s, e = out.find("{"), out.rfind("}")
        lab = json.loads(out[s:e + 1]) if s != -1 else {}
    except Exception:  # noqa: BLE001
        lab = {}
    ref = {
        "ref_id": _next_ref_id(refs),
        "caption": cap,
        "why_it_works": (lab.get("why_it_works") or "").strip() or None,
        "primary_lever": (lab.get("primary_lever") or "shareability").strip(),
        "secondary_levers": [],
        "persona": "core_persona",
        "persona_trait": (lab.get("persona_trait") or "core_voice").strip(),
        "format": "single",
        "clip_dependency": "none",
        "metrics": None,
        "source": source,
        "promoted_from": [a for a in (anchors or []) if a],
        "rating": rating,
        "notes": note,
    }
    path = profiles.corpus_path(pid)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ref, ensure_ascii=False) + "\n")
    return ref["ref_id"]


def _rec_voice(rec: dict):
    """The VOICE that generated this reel (recorded at generation; None -> current voice pointer)."""
    import uuid as _uuid
    v = rec.get("voice_profile_id")
    try:
        return _uuid.UUID(v) if v else None
    except (ValueError, TypeError):
        return None


def promote(reel_id: str, pid=None) -> dict:
    """Promote ONE graded reel's posted caption into the corpus OF THE VOICE that generated it."""
    rec = next((r for r in reel_store.graded(pid) if r.get("reel_id") == reel_id), None)
    if rec is None:
        return {"ok": False, "reason": "reel not found or ungraded"}
    rating = (rec.get("grade") or {}).get("rating") or 0
    rid = _add_ref(rec.get("caption") or "", rating, rec.get("caption_anchor_refs") or [], "promoted_gen",
                   f"operator-rated {rating}/10 on a real reel; promoted into the corpus", _rec_voice(rec))
    reel_store.mark_promoted(reel_id, pid)
    return {"ok": True, "ref_id": rid, "already": rid is None}


_ENDORSE_RX = re.compile(r"would(?:'ve| have| of)? been (?:like )?(?:a |an )?(\d{1,2})", re.IGNORECASE)


def promote_all(pid=None, min_rating: int = 8) -> dict:
    """THE learning flow: every operator-validated line enters the corpus automatically — posted reels
    rated >= min_rating, note-endorsed alts ("[X] would have been an 8/9", text-matched to the reel's
    real candidates by the pairwise mining, per-line claim honored when the miner captured one), AND
    operator-AUTHORED captions written inside grading notes (matched to NO candidate — the operator
    literally writing the voice; highest-provenance grounding there is). The grades ARE the gate;
    idempotent via dedup. The endorsed line's anchor also gets a keep credit so the formats producing
    operator-endorsed lines amplify in rotation."""
    from app.corpus import attribute
    from app.corpus import grades as grade_store
    all_grades = grade_store.load_grades()
    pair_winners = {_norm(g.get("winner") or "") for g in all_grades if g.get("type") == "pairwise"}
    # per-line claims captured by the note miner — a note claiming "an 8" AND "a 10" must not stamp
    # both endorsed lines with the max
    line_claims: dict[str, int] = {}
    for g in all_grades:
        if g.get("type") == "pairwise" and isinstance((g.get("context") or {}).get("claim"), int):
            k = _norm(g.get("winner") or "")
            line_claims[k] = max(line_claims.get(k, 0), g["context"]["claim"])
    posted, endorsed, authored = [], [], []
    for r in reel_store.graded(pid):
        g = r.get("grade") or {}
        rating = g.get("rating") or 0
        if rating >= min_rating and not r.get("promoted"):
            res = promote(r.get("reel_id"), pid)
            if res.get("ref_id"):
                posted.append(res["ref_id"])
        note_claim = max((int(x) for x in _ENDORSE_RX.findall(g.get("notes") or "")), default=0)
        if note_claim >= min_rating:
            vpid = _rec_voice(r)
            for c in (r.get("candidates") or []):
                if not c.get("chosen") and _norm(c.get("text") or "") in pair_winners:
                    claim = line_claims.get(_norm(c.get("text") or "")) or note_claim
                    if claim < min_rating:
                        continue
                    rid = _add_ref(c.get("text") or "", claim,
                                   [c.get("anchor_ref")], "note_endorsed",
                                   f"operator note: would have been a {claim}; promoted into the corpus", vpid)
                    if rid:
                        endorsed.append(rid)
                        if c.get("anchor_ref"):   # amplify the format that produced the endorsed line
                            attribute.credit_verdict({"anchor_refs": [c["anchor_ref"]]}, "keep", vpid)
    for g in all_grades:
        if g.get("type") == "authored" and (g.get("claim") or 0) >= min_rating:
            rid = _add_ref(g.get("caption") or "", g.get("claim") or 0, [], "operator_authored",
                           f"operator-AUTHORED in a grading note (claimed {g.get('claim')}/10) — "
                           "ground-truth voice", pid)
            if rid:
                authored.append(rid)
    return {"posted_promoted": len(posted), "endorsed_promoted": len(endorsed),
            "authored_promoted": len(authored), "ref_ids": posted + endorsed + authored}
