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


def promote(reel_id: str, pid=None) -> dict:
    """Promote ONE graded reel's caption into the reference corpus (operator-gated; idempotent)."""
    rec = next((r for r in reel_store.graded(pid) if r.get("reel_id") == reel_id), None)
    if rec is None:
        return {"ok": False, "reason": "reel not found or ungraded"}
    cap = (rec.get("caption") or "").strip()
    rating = (rec.get("grade") or {}).get("rating") or 0
    if not cap:
        return {"ok": False, "reason": "blank caption"}
    refs = load_refs(profiles.corpus_path(pid))
    if _norm(cap) in {_norm(r.get("caption") or "") for r in refs}:
        reel_store.mark_promoted(reel_id, pid)
        return {"ok": True, "already": True}

    try:    # decode the execution principles (why THIS rendition lands) — the learning content
        out = complete_json(_LABEL_SYS, f"CAPTION:\n{cap}", effort="high", max_tokens=600)
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
        "source": "promoted_gen",
        "promoted_from": rec.get("caption_anchor_refs") or [],
        "rating": rating,
        "notes": f"operator-rated {rating}/10 on a real reel; promoted into the corpus",
    }
    path = profiles.corpus_path(pid)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ref, ensure_ascii=False) + "\n")
    reel_store.mark_promoted(reel_id, pid)
    return {"ok": True, "ref_id": ref["ref_id"], "why_it_works": ref["why_it_works"]}
