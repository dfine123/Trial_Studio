"""THE LAB — generation from PRINCIPLES, not references.

Production generates reference-DOMINATED (each line sparked from a rotated corpus ref) —
correct for consistency, but structurally format-bound: at scale it re-treads shapes. The
lab is the opposite pole, built per the operator's architecture: EXTRACT + CONSOLIDATE the
principles from everything we know about what hits — every reference's decoded
why_it_works, every graded reel (the 8-10s AND the operator's own kill notes: "flat
delivery", "corny", "lame premise"), the persona — into a CODEX of mechanisms (cached,
rebuildable), then generate with NO reference wall at all: persona + mechanics + codex.
Extrapolation from understanding is the only path available. A line that reads as a
re-skin of anything already done is defined as a FAILURE. Bar: catalog-worthy — the lab's
hits should out-hit production.

⚠️ LESSONS (operator corrections, 2026-07-04 — both mine):
1. Never brief exploration as license to miss ("a glorious miss beats a safe 7" aimed at
   novelty and produced intentionally-experimental lines).
2. Never ground the lab in the reference WALL — the model gravitates to recreating the
   formats it sees ("raccoon but pigeon"), which is redundancy at the meta level and "does
   nothing for us". Principles-basis is the point: consolidate WHY things hit, generate
   from that understanding.

Isolation: its own pool log (voice-owned lab_pool.jsonl), NO production genlog writes, NO
rotation credit, NO reels — with exactly ONE designed pathway back into the system: a lab
line the operator rates >=8 auto-promotes into the ACTIVE VOICE's references
(source=lab_promoted, near-dup guarded, why_it_works-decoded) — which then feeds the NEXT
codex rebuild. Understanding compounds.
"""
from __future__ import annotations

import hashlib
import json
import os
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


# ── THE CODEX: consolidated principles extracted from every ref decoding + every grade ──
_CODEX_SYS = """You are distilling the OPERATING PRINCIPLES of one creator's caption voice from hard evidence: his reference catalog (each line with a decoded note on why it landed) and an operator's real grades — lines that HIT (rated 8-10) and lines that MISSED (rated 1-4, with the operator's own notes on exactly why). Produce THE CODEX: the consolidated understanding a writer would need to produce NEW lines at the catalog's peak level — written to GENERATE from, never to imitate from.

HARD RULES:
- Decode MECHANISMS, never formats. No cataloging of shapes or templates of any kind. Every principle must hold across ANY shape a line could take.
- Every principle must EXPAND what is writable — generative, opening territory — never a restriction to what has already been done.
- Ground everything in the evidence (quote short fragments as proof where it sharpens the point), but the output is UNDERSTANDING, not examples to copy.

Sections:
1. THE CORE — the position and psychology that makes this voice hit: where he stands relative to the subject and the reader, what the reader FEELS in the half-second after the line lands, and why they send it to someone.
2. THE CRAFT — how the surprise is actually constructed; what specificity is DOING (proof, not decoration); the economy logic (what earns its place); where and how the turn detonates; what separates a payoff that SNAPS from one that lands flat.
3. THE TRIPWIRES — the operator's own named failure modes, decoded as mechanisms: what is mechanically happening when a line reads "flat delivery", "corny", "lame/normie premise", "off landing" — so a writer feels the failure coming WHILE writing, not as a list of bans.
4. EIGHT VS TEN — what the highest-rated lines do that merely-good lines don't.

Dense, direct, second person ("your lines…"), ~700 words. Return ONLY the codex text."""


def _codex_path() -> str:
    return profiles.voice_file(profiles._suffixed("lab_codex.md"), profiles.voice_id())


def build_codex(force: bool = False) -> dict:
    """Extract + consolidate the principles from all evidence into the cached codex.
    Rebuild after learn runs (new refs + grades = new evidence)."""
    from app.corpus import reels as reel_store
    from app.corpus.store import load_refs
    path = _codex_path()
    if not force and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return {"codex": f.read(), "rebuilt": False}
    refs = load_refs()
    graded = reel_store.graded()
    ref_lines = []
    for r in refs:
        cap = (r.get("caption") or "").strip()
        if not cap:
            continue
        why = (r.get("why_it_works") or "").strip()
        ref_lines.append(f"- {cap}" + (f"\n  (why it landed: {why})" if why else ""))
    hits, misses = [], []
    for rec in graded:
        g = rec.get("grade") or {}
        rating = g.get("rating") or 0
        cap = (rec.get("caption") or "").strip().replace("\n", " / ")
        note = (g.get("notes") or "").strip()
        if not cap:
            continue
        if rating >= 8:
            hits.append(f"- [{rating}/10] {cap}" + (f" — operator: {note}" if note else ""))
        elif rating <= 4 and note:
            misses.append(f"- [{rating}/10] {cap} — operator: {note}")
    user = (
        "WHO HE IS:\n" + persona() + "\n\n"
        "THE CATALOG (posted references + decoded why-it-works):\n" + "\n".join(ref_lines) + "\n\n"
        "GRADED HITS (operator rated 8-10):\n" + ("\n".join(hits) or "(none yet)") + "\n\n"
        "GRADED MISSES (operator rated 1-4, with the operator's own reason):\n" + ("\n".join(misses) or "(none yet)")
    )
    codex = complete_json(_CODEX_SYS, user, effort="high", max_tokens=2600, tag="lab-codex").strip()
    if not codex:
        raise RuntimeError("codex distillation returned nothing")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(codex)
    os.replace(tmp, path)
    return {"codex": codex, "rebuilt": True, "refs": len(ref_lines), "hits": len(hits), "misses": len(misses)}


def persona() -> str:
    from app.caption.engine import persona as _p
    return _p()


_LAB_BRIEF = (
    "THE LAB. No references in front of you, no formats to lean on — you are writing from "
    "UNDERSTANDING. The codex above is the distilled reason your lines hit, extracted from "
    "everything you've posted and every grade you've received. Write {n} NEW lines from those "
    "principles: fresh premises, fresh angles, whatever shape each idea itself demands — invent "
    "the shape if the idea calls for one. A line that reads like a re-skin of anything you've "
    "done before is a FAILURE here. The bar is catalog-worthy: swings that hit harder than your "
    "production system reaches, lines that would earn a permanent spot. Each of the {n} takes a "
    "genuinely different swing. Only ship what you'd bet on."
)


def generate_lab(n: int = 8) -> list[dict]:
    """n swings generated from PRINCIPLES (persona + mechanics + codex — no reference wall), in
    one call. The codex builds automatically on first use. Isolated (no prod genlog)."""
    from app.caption.engine import _MECHANICS, _avoid_block, _drop_ref_copies
    codex = build_codex().get("codex") or ""
    # anti-redundancy: territory production + the lab already covered — exploration goes NEW places
    lab_stubs = [" ".join(((r.get("text") or "").replace("\n", " / ")).split()[:9])
                 for r in _load_pool()[-120:]]
    avoid = _avoid_block() + ("\n" + "\n".join("- " + s + "…" for s in dict.fromkeys(lab_stubs)) if lab_stubs else "")

    system = (persona() + "\n\n" + _MECHANICS + "\n\nTHE CODEX — the distilled understanding of "
              "why your lines hit:\n\n" + codex)
    user = (
        _LAB_BRIEF.replace("{n}", str(n)) + "\n\n"
        f"(Territory already covered — go somewhere NEW: {avoid})\n\n"
        f"Write {n} captions. ONLY JSON, no prose: "
        '{"captions": ["caption 1 (\\n for line breaks)", "caption 2", "..."]}'
    )
    # 8000: adaptive thinking spends from the same budget as the JSON — 4000 truncated mid-batch
    # (measured out=4000 exactly); billed only as used
    text = complete_json(system, user, effort="high", max_tokens=8000, tag="lab")
    s, e = text.find("{"), text.rfind("}")
    cands = []
    if s != -1 and e != -1:
        try:
            cands = [{"text": (t or "").strip(), "anchors": []}
                     for t in json.loads(text[s:e + 1]).get("captions", []) if (t or "").strip()]
        except json.JSONDecodeError:
            cands = []
    out = _drop_ref_copies(cands)        # writing from understanding must still never reproduce a catalog line
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
            "hits": sum(1 for r in rated if (r.get("rating") or 0) >= 8),
            "codex_built": os.path.exists(_codex_path())}
