"""THE LAB — production's restrictions OFF, the bar HIGHER.

Production generation locks each candidate to ONE rotation-assigned reference (coverage
fairness) and edits the output. The lab strips those restrictions: the model gets the whole
catalog at once, full freedom over angle/format/territory — in service of lines that hit
HARDER than production can reach (catalog-worthy kill shots), never novelty for its own
sake. Same voice grounding (persona + references + mechanics — it must still be him); no
refine pass (the raw edge ships); anti-repeat kept as "covered territory — go somewhere new".

⚠️ LESSON (operator correction, 2026-07-04): the first version briefed "a glorious miss
beats a safe 7" + forced distant-reference collisions — that AIMED AT novelty and licensed
misses, producing intentionally-experimental lines. Exploration is the MEANS; peak quality
is the TARGET. The lab's hits should out-hit production, not excuse weaker output.

Isolation: its own pool log (voice-owned lab_pool.jsonl), NO production genlog writes, NO
rotation credit, NO reels — with exactly ONE designed pathway back into the system: a lab
line the operator rates >=8 auto-promotes into the ACTIVE VOICE's references
(source=lab_promoted, near-dup guarded) — grounding by wins, the living corpus.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time

from app import profiles
from app.caption.llm import complete_json


def _pool_path() -> str:
    return profiles.lab_pool_path()


def _load_pool() -> list[dict]:
    path = _pool_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _append_pool(rows: list[dict]) -> None:
    path = _pool_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rewrite_pool(rows: list[dict]) -> None:
    path = _pool_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _cid(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode()).hexdigest()[:12]


_LAB_BRIEF = (
    "THE LAB. In production you write each line SPARKED FROM one assigned reference, rotated for "
    "coverage. Tonight that restriction is OFF: the whole catalog above is yours at once, and the "
    "bar is HIGHER here, not lower — your job is lines that hit HARDER than the production system "
    "can reach, lines so good they'd earn a permanent spot in the catalog. Go wherever the heat "
    "is: corners of the catalog production's rotation rarely reaches, collisions between its "
    "worlds, formats and angles it hasn't touched — exploration in service of the KILL SHOT, "
    "never novelty for its own sake. Every line must be unmistakably HIM (the confidence, the "
    "precision, the economy — never a different person), and each of the {n} must be a genuinely "
    "different swing — different angle, shape, or territory. Only ship swings you'd bet on."
)


def generate_lab(n: int = 8) -> list[dict]:
    """n unleashed swings in ONE call: full-catalog freedom (no anchor lock, no rotation), the
    model self-diversifies the set — the bar is production-plus, not experimental. Isolated
    (no prod genlog)."""
    from app.caption.engine import _avoid_block, _drop_ref_copies, voice_system
    from app.corpus.store import load_refs
    refs = load_refs()
    if not refs:
        raise RuntimeError("this voice has no references yet — the lab needs a corpus to explode")
    random.shuffle(refs)
    ref_block = "\n\n".join((r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip())
    # anti-redundancy: territory production + the lab already covered — exploration goes NEW places
    lab_stubs = [" ".join(((r.get("text") or "").replace("\n", " / ")).split()[:9])
                 for r in _load_pool()[-120:]]
    avoid = _avoid_block() + ("\n" + "\n".join("- " + s + "…" for s in dict.fromkeys(lab_stubs)) if lab_stubs else "")

    user = (
        _LAB_BRIEF.replace("{n}", str(n)) + "\n\n"
        f"(Territory already covered — go somewhere NEW: {avoid})\n\n"
        f"Write {n} captions. ONLY JSON, no prose: "
        '{"captions": ["caption 1 (\\n for line breaks)", "caption 2", "..."]}'
    )
    text = complete_json(voice_system(ref_block), user, effort="high", max_tokens=4000, tag="lab")
    s, e = text.find("{"), text.rfind("}")
    cands = []
    if s != -1 and e != -1:
        try:
            cands = [{"text": (t or "").strip(), "anchors": []}
                     for t in json.loads(text[s:e + 1]).get("captions", []) if (t or "").strip()]
        except json.JSONDecodeError:
            cands = []
    out = _drop_ref_copies(cands)        # an unleashed swing must still not be a catalog line verbatim
    rows = [{"caption_id": _cid(c["text"]), "text": c["text"], "anchors": c.get("anchors") or [],
             "ts": time.time(), "rating": None} for c in out]
    _append_pool(rows)                   # the lab's OWN log — production genlog untouched
    return rows


def grade_lab(caption_id: str, rating: int) -> dict:
    """Rate a lab line. >=8 crosses the one designed bridge: auto-promoted into the ACTIVE VOICE's
    references (source=lab_promoted, near-dup guarded). Everything else just records."""
    rows = _load_pool()
    rec = next((r for r in rows if r.get("caption_id") == caption_id), None)
    if rec is None:
        return {"ok": False, "reason": "unknown caption_id"}
    rec["rating"] = int(rating)
    promoted = None
    if int(rating) >= 8:
        from app.corpus.promote import _add_ref
        promoted = _add_ref(rec.get("text") or "", int(rating), rec.get("anchors") or [],
                            "lab_promoted", f"operator-rated {int(rating)}/10 in the lab (exploration lane)")
        rec["promoted_ref"] = promoted
    else:
        # re-graded below the bar: the row no longer claims promotion (an already-promoted ref
        # stays in the corpus until removed via /api/debug/corpus-remove — the corpus is curated,
        # not auto-culled)
        rec["promoted_ref"] = None
    _rewrite_pool(rows)
    return {"ok": True, "promoted": promoted}


def lab_stats() -> dict:
    rows = _load_pool()
    rated = [r for r in rows if r.get("rating") is not None]
    return {"generated": len(rows), "rated": len(rated),
            "promoted": sum(1 for r in rows if r.get("promoted_ref")),
            "hits": sum(1 for r in rated if (r.get("rating") or 0) >= 8)}
