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
- Decode PRINCIPLES, not templates. Never output a catalog of his existing shapes to refill — but you MUST decode what FORM and TEXTURE do in this voice, because the shape and the sound ARE part of the jokes. The test: a principle should let a writer invent a shape he's never used and have it still be unmistakably his.
- Every principle must EXPAND what is writable — generative, opening territory — never a restriction to what has already been done.
- Ground everything in the evidence (quote short fragments as proof where it sharpens the point), but the output is UNDERSTANDING, not examples to copy.

Sections:
1. THE CORE — the position and psychology that makes this voice hit: where he stands relative to the subject and the reader, what the reader FEELS in the half-second after the line lands, and why they send it to someone.
2. THE CRAFT — how the surprise is actually constructed; what specificity is DOING (proof, not decoration); the economy logic; where the turn detonates; what separates a payoff that SNAPS from one that lands flat.
3. THE FORM — this voice PLAYS with structure; flat declarative observation is its death. Decode what the structural play is DOING: what it means to drop the reader mid-scene, to let the punch be said TO someone, to build a trap the reader walks into, to spike the energy, to let the shape itself be half the joke — and what makes an INVENTED shape still his. Function of form, never a template list.
4. THE TEXTURE — the surface DNA he actually types in: the casing, the slang register and when it's load-bearing, emoji as punctuation/energy, caps as volume, timing via line breaks. Decode why a clean, essay-grade sentence reads as someone else — what "written" sounds like vs what HE sounds like.
5. THE TRIPWIRES — the operator's own named failure modes, decoded as mechanisms: what is mechanically happening when a line reads "flat delivery", "corny", "lame/normie premise", "off landing" — so a writer feels the failure coming WHILE writing.
6. EIGHT VS TEN — what the highest-rated lines do that merely-good lines don't.

Dense, direct, second person ("your lines…"), ~900 words. Return ONLY the codex text."""


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
    hits, misses, mids = [], [], []
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
        elif 5 <= rating <= 7 and note:
            # the mid band was a structural blind spot: operator notes here often ENDORSE a format
            # or premise wearing a failed execution ("great format but lame premise", a handed-over
            # template) — evidence that previously reached nothing
            mids.append(f"- [{rating}/10] {cap} — operator: {note}")
    user = (
        "WHO HE IS:\n" + persona() + "\n\n"
        "THE CATALOG (posted references + decoded why-it-works):\n" + "\n".join(ref_lines) + "\n\n"
        "GRADED HITS (operator rated 8-10):\n" + ("\n".join(hits) or "(none yet)") + "\n\n"
        "GRADED MISSES (operator rated 1-4, with the operator's own reason):\n" + ("\n".join(misses) or "(none yet)") + "\n\n"
        "GRADED NEAR-MISSES (operator rated 5-7 WITH a note — read these notes closely: they often "
        "VALIDATE a format, premise or template while killing the execution; mine what the operator "
        "endorsed, not just what failed):\n" + ("\n".join(mids) or "(none yet)")
    )
    codex = complete_json(_CODEX_SYS, user, effort="high", max_tokens=2600, tag="lab-codex").strip()
    if not codex:
        raise RuntimeError("codex distillation returned nothing")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(codex)
    os.replace(tmp, path)
    return {"codex": codex, "rebuilt": True, "refs": len(ref_lines), "hits": len(hits),
            "misses": len(misses), "mids": len(mids)}


def persona() -> str:
    from app.caption.engine import persona as _p
    return _p()


_IDEATE_SYS = """You generate IDEAS for one creator's captions — and an idea here is a PREMISE plus its PLAY. You work from THE CODEX (the distilled understanding of why his lines hit, how his forms work, how he sounds) and a list of TAKEN territory (every premise his catalog and recent output already covers).

A matter-of-fact observation is the FAILURE MODE of this job. Every idea must carry PLAY — the bit, the move, the shape that makes it something he'd actually post rather than a statement:
- PREMISE: fresh, specific, charged (a cope being run right now, a category about to collapse, a doubter who needs ammo, a moment with tension in it) — territory his catalog has NEVER touched, never a topic label, never a rewording of anything taken.
- PLAY: how it's DELIVERED — a scene the reader gets dropped into, an exchange where the punch is said to someone, a build the reader walks into, an energy spike, a structure nobody's used before. INVENT shapes; the codex's form principles tell you what makes a shape his. The play and the premise should need each other.

Range across his whole world (money, women, status, the grind, family, the internet) AND past it — the codex tells you what makes territory HIS; trust it into new rooms. Each idea genuinely different from the others in BOTH premise and play.

Return ONLY JSON: {"ideas": [{"premise": "the specific charged observation/moment", "play": "the delivery — the bit/shape/move that makes it a post", "charge": "which codex mechanism lands it"}]}"""

_EXECUTE_SYS_TAIL = """

THE TASK: below are {k} locked IDEAS — each a PREMISE plus its PLAY, ideated from your codex. The premises are FIXED (never swap, merge, or drift them); the play tells you the delivery — commit to it or invent a sharper shape for the same premise. Your catalog above is the BAR and the sound-check, not source material: its premises are taken, but it shows exactly how you actually type — the casing, the slang, the emoji, the energy, the timing. Write each idea AS A POST, in that exact texture. If a draft reads like a clean observation an essayist could have written, it's DEAD — that's not you; rewrite it as the bit it wants to be. Write the strongest {n} of the {k}; drop ideas you can't make catalog-topping. Only ship what you'd bet on.

Return ONLY JSON, no prose: {"captions": ["caption (\\n for line breaks)", "..."]}"""


def generate_lab(n: int = 8) -> list[dict]:
    """Two stages, per the operator's architecture + the grounding canon:
    A) IDEATE from principles — codex only, ZERO references in context, catalog premises marked
       as taken → premises structurally cannot be re-skins (the topic is fixed before any
       reference is seen).
    B) EXECUTE at the catalog bar — the full reference wall returns purely as CRAFT calibration
       (what snap feels like at full fidelity); it cannot hijack topics because they're locked.
    Isolated (no prod genlog)."""
    from app.caption.engine import _MECHANICS, _avoid_block, _drop_ref_copies
    from app.corpus.store import load_refs
    codex = build_codex().get("codex") or ""
    refs = load_refs()
    # TAKEN territory for ideation: catalog premises + recent production + the lab's own pool
    ref_stubs = [" ".join(((r.get("caption") or "").replace("\n", " / ")).split()[:9]) for r in refs]
    lab_stubs = [" ".join(((r.get("text") or "").replace("\n", " / ")).split()[:9])
                 for r in _load_pool()[-120:]]
    taken = "\n".join("- " + s + "…" for s in dict.fromkeys(x for x in ref_stubs + lab_stubs if x))
    avoid_prod = _avoid_block()

    # ── stage A: premises from principles (no references anywhere in context) ──
    k = n + 4   # overgenerate; stage B writes only the strongest n
    a_user = (
        f"THE CODEX:\n\n{codex}\n\n"
        f"TAKEN TERRITORY — every one of these premises is used; yours must live elsewhere:\n{taken}\n"
        f"{avoid_prod}\n\n"
        f"Generate {k} premises."
    )
    a_out = complete_json(_IDEATE_SYS, a_user, effort="high", max_tokens=16000, tag="lab-ideate")
    s, e = a_out.find("{"), a_out.rfind("}")
    premises = []
    if s != -1 and e != -1:
        try:
            premises = [p for p in json.loads(a_out[s:e + 1]).get("ideas", [])
                        if (p.get("premise") or "").strip()]
        except json.JSONDecodeError:
            premises = []
    if not premises:
        raise RuntimeError("ideation returned no ideas — check the codex")

    # ── stage B: execution with the wall as the BAR (premises locked, topics can't be hijacked) ──
    ref_block = "\n\n".join((r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip())
    system = (persona() + "\n\n" + _MECHANICS
              + "\n\nTHE CODEX — why your lines hit:\n\n" + codex
              + "\n\nYOUR CATALOG (the bar to clear; premises taken):\n\n" + ref_block
              + _EXECUTE_SYS_TAIL.replace("{k}", str(len(premises))).replace("{n}", str(n)))
    b_user = "LOCKED IDEAS:\n" + "\n".join(
        f"[{i}] PREMISE: {p['premise']}"
        + (f"\n    PLAY: {p.get('play')}" if p.get("play") else "")
        + (f"\n    (charge: {p.get('charge')})" if p.get("charge") else "")
        for i, p in enumerate(premises)
    ) + f"\n\nWrite the strongest {n}, each as the post it wants to be."
    text = complete_json(system, b_user, effort="high", max_tokens=8000, tag="lab-write")
    s, e = text.find("{"), text.rfind("}")
    cands = []
    if s != -1 and e != -1:
        try:
            cands = [{"text": (t or "").strip(), "anchors": []}
                     for t in json.loads(text[s:e + 1]).get("captions", []) if (t or "").strip()]
        except json.JSONDecodeError:
            cands = []
    out = _drop_ref_copies(cands)        # execution must still never reproduce a catalog line
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
