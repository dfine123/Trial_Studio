"""THE LAB — an isolated exploration lane for caption generation.

Production generation is reference-DOMINATED and convergent by design; at scale that reads
"a tiny bit redundant" (operator). The lab runs HOT on purpose: the SAME voice grounding
(persona + references + mechanics — it must still be him), but each candidate collides TWO
deliberately-DISTANT references and the brief licenses the swing: write the line the corpus
wouldn't predict. New formats welcome; a glorious miss beats a safe 7.

Isolation: its own pool log (voice-owned lab_pool.jsonl), NO production genlog writes, NO
rotation credit, NO reels — with exactly ONE designed pathway back into the system: a lab
line the operator rates >=8 auto-promotes into the ACTIVE VOICE's references
(source=lab_promoted, near-dup guarded) — grounding by wins, the living corpus.

Heat note: the Anthropic API caps temperature at 1.0 (and adaptive thinking locks it), so
the lab's heat is ENGINEERED — distant-anchor recombination + an exploration brief — not a
sampling knob.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

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


def _words(t: str) -> set[str]:
    return {w for w in "".join(ch if ch.isalnum() else " " for ch in (t or "").lower()).split() if len(w) > 3}


def _distant_pairs(refs: list[dict], n: int) -> list[tuple[dict, dict]]:
    """n anchor PAIRS, each deliberately far apart: a random ref + the ref (from a sample) sharing
    the fewest content words with it. Distant DNA -> recombination pressure -> lines the corpus
    wouldn't predict."""
    pool = [r for r in refs if (r.get("caption") or "").strip()]
    pairs = []
    for _ in range(n):
        a = random.choice(pool)
        aw = _words(a.get("caption"))
        sample = random.sample(pool, min(40, len(pool)))
        b = min((r for r in sample if r is not a),
                key=lambda r: len(aw & _words(r.get("caption"))), default=a)
        pairs.append((a, b))
    return pairs


_LAB_BRIEF = (
    "THE LAB — tonight you're off the leash. You know this voice cold (the references above are "
    "yours); now write the line the references WOULDN'T predict. Below are TWO of your old lines "
    "with unrelated DNA — collide them: take the mechanism of one into the world of the other, or "
    "find the third thing neither of them saw. New formats and shapes you've never used are "
    "welcome. Weird, hyper-specific swings beat safe competent lines — a glorious miss beats a "
    "safe 7. It still has to be HIM (the confidence, the specificity, the economy) — exploration "
    "of WHAT gets said and HOW it's shaped, never a different person."
)


def generate_lab(n: int = 8) -> list[dict]:
    """n hot candidates, each from a distant-anchor collision. Parallel; isolated (no prod genlog)."""
    from app.caption.engine import _avoid_block, _drop_ref_copies, voice_system
    from app.corpus.store import load_refs
    refs = load_refs()
    if not refs:
        raise RuntimeError("this voice has no references yet — the lab needs a corpus to explode")
    random.shuffle(refs)
    ref_block = "\n\n".join((r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip())
    # anti-redundancy: production stubs (don't re-make what prod already made) + the lab's own pool
    lab_stubs = [" ".join(((r.get("text") or "").replace("\n", " / ")).split()[:9])
                 for r in _load_pool()[-120:]]
    avoid = _avoid_block() + ("\n" + "\n".join("- " + s + "…" for s in dict.fromkeys(lab_stubs)) if lab_stubs else "")
    pairs = _distant_pairs(refs, n)

    def one(pair: tuple[dict, dict]) -> dict | None:
        a, b = pair
        user = (
            _LAB_BRIEF + "\n\n"
            f"LINE A: {a.get('caption')}\n(why it landed: {a.get('why_it_works') or '—'})\n\n"
            f"LINE B: {b.get('caption')}\n(why it landed: {b.get('why_it_works') or '—'})\n\n"
            f"(Don't rehash these recent premises: {avoid})\n\n"
            'Write ONE caption. ONLY JSON, no prose: {"text": "the caption (\\n for line breaks)"}'
        )
        text = complete_json(voice_system(ref_block), user, effort="high", max_tokens=1500,
                             cache_system=True, tag="lab")   # collisions share one system — cache it
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e == -1:
            return None
        try:
            t = (json.loads(text[s:e + 1]).get("text") or "").strip()
        except json.JSONDecodeError:
            return None
        return {"text": t, "anchors": [a.get("ref_id"), b.get("ref_id")]} if t else None

    # sequential-first (see engine.generate_independent): collision 1 pays the single cache write,
    # the rest fan out and READ at ~10%
    raw = []
    if pairs:
        first = contextvars.copy_context().run(one, pairs[0])
        if first:
            raw.append(first)
        if len(pairs) > 1:
            with ThreadPoolExecutor(max_workers=max(1, n)) as ex:
                futs = [ex.submit(contextvars.copy_context().run, one, p) for p in pairs[1:]]
                raw += [c for c in (f.result() for f in futs) if c]
    out = _drop_ref_copies(raw)          # a collision must not return a parent verbatim
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
