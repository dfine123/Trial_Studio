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

_LABEL_SYS = """You are annotating ONE caption for a creator's reference corpus — operator-rated as the voice at its best. Your job is NOT to extract a reusable format. It is to transfer TASTE: what makes this line STRONG where its almost-identical neighbor would be weak, corny, or senseless. Write as a sharp writer reading a line he admires — never as an engineer reverse-engineering a template.

- why_it_works: ≤ 50 words, punchy, in the creator's own blunt idiom. Cover whatever mix THIS line demands of: why the PREMISE carries charge before the wording even arrives (the subject pulls its own weight); why it makes sense in ONE read (the logic clicks with nothing re-assembled); and what the DELIVERY refuses to do that the corny version of this exact line would have done — name what it dodged. NEVER describe the shape ("quote then flip", "X then Y", "template", "format") — describe the strength.
- why_full: the same lens at full depth: the taste calls inside the line (the word chosen, what's left unsaid, where it stops), why this subject is strong where a nearby subject would be weak, and what an 8/10 version of this same idea would have gotten wrong. Faithful to THIS line, never generic advice, never a recipe.

Also give a precise persona_trait (open vocabulary, e.g. shameless_villain, self_aware_hustler, deadpan_crude, absurd_motivational, deep_bro_sincere, anticope_callout), a primary_lever (e.g. shareability, comment_bait, iykyk_decode, relatability), and generativity: "generative" if the line's STRENGTH transposes to fresh subjects; "singular" if it won on a one-time collision that doesn't transpose. One judgment, no hedging.

⚠️ Both decode fields are rendered inside the creator's own voice prompt as self-knowledge. If you're given the operator's note, fold its INSIGHT in as plain understanding of the line — but NEVER mention the operator, a note, feedback, grades, or ratings in EITHER field.

Return ONLY JSON: {"why_it_works": "...", "why_full": "...", "persona_trait": "...", "primary_lever": "...", "generativity": "generative" | "singular"}"""


def _norm_generativity(v) -> str | None:
    v = (v or "").strip().lower() if isinstance(v, str) else ""
    return v if v in ("generative", "singular") else None


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


def _add_ref(caption: str, rating: int, anchors: list, source: str, note: str, pid=None,
             op_note: str | None = None) -> str | None:
    """Append one operator-validated caption to the corpus (deduped, incl. NEAR-duplicates — a format
    must never stack multiple copies of the same joke, or it gets double the rotation slots + double
    the voice-block priming). op_note = the operator's own grading note, fed to the why_it_works
    labeler so the decode carries THEIR read (e.g. a punch-up they wrote), not just the LLM's.
    Returns the new ref_id or None."""
    cap = (caption or "").strip()
    if not cap:
        return None
    refs = load_refs(profiles.corpus_path(pid))
    if _norm(cap) in {_norm(r.get("caption") or "") for r in refs}:
        return None
    if any(_too_similar(cap, r.get("caption") or "") for r in refs):
        return None   # same joke, different rendition — one copy is enough
    user = f"CAPTION:\n{cap}"
    if (op_note or "").strip():
        user += f"\n\nOPERATOR'S OWN NOTE on it (their read outranks yours — fold it in):\n{op_note.strip()}"
    try:    # decode the execution principles (why THIS rendition lands) — the learning content
        out = complete_json(_LABEL_SYS, user, effort="high", max_tokens=1200, tag="promote-label")
        s, e = out.find("{"), out.rfind("}")
        lab = json.loads(out[s:e + 1]) if s != -1 else {}
    except Exception:  # noqa: BLE001
        lab = {}
    ref = {
        "ref_id": _next_ref_id(refs),
        "caption": cap,
        "why_it_works": (lab.get("why_it_works") or "").strip() or None,
        # the split (permanent architecture): why_it_works = short, ANCHOR-facing (rendered as WHY IT
        # LANDS inside the voice); why_full = rich, CONSOLIDATION-facing (codex evidence only)
        "why_full": (lab.get("why_full") or "").strip() or None,
        "generativity": _norm_generativity(lab.get("generativity")),
        "decode_v": 2,
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
                   f"operator-rated {rating}/10 on a real reel; promoted into the corpus", _rec_voice(rec),
                   op_note=(rec.get("grade") or {}).get("notes"))
    reel_store.mark_promoted(reel_id, pid)
    return {"ok": True, "ref_id": rid, "already": rid is None}


def relabel(ref_ids: list[str], pid=None) -> dict:
    """Re-decode why_it_works for specific refs WITH the operator's grading note folded in — for refs
    promoted before the op_note wiring existed, or whose label call failed silently (p062 shipped with
    why_it_works=None). Finds each ref's source note by matching its caption against every graded
    reel's posted caption AND its candidate list (endorsed refs live in candidates)."""
    refs = load_refs(profiles.corpus_path(pid))
    notes: dict[str, str] = {}
    for r in reel_store.graded(pid):
        note = ((r.get("grade") or {}).get("notes") or "").strip()
        if not note:
            continue
        caps = [r.get("caption") or ""] + [(c.get("text") or "") for c in (r.get("candidates") or [])]
        for cap in caps:
            if cap.strip():
                notes.setdefault(_norm(cap), note)
    changed = []
    want = set(ref_ids)
    for ref in refs:
        if ref.get("ref_id") not in want:
            continue
        cap = (ref.get("caption") or "").strip()
        op_note = notes.get(_norm(cap))
        user = f"CAPTION:\n{cap}"
        if op_note:
            user += f"\n\nOPERATOR'S OWN NOTE on it (their read outranks yours — fold it in):\n{op_note}"
        try:
            out = complete_json(_LABEL_SYS, user, effort="high", max_tokens=1200, tag="relabel")
            s, e = out.find("{"), out.rfind("}")
            lab = json.loads(out[s:e + 1]) if s != -1 else {}
        except Exception:  # noqa: BLE001
            lab = {}
        why = (lab.get("why_it_works") or "").strip()
        if not why:
            continue
        ref["why_it_works"] = why
        if (lab.get("why_full") or "").strip():
            ref["why_full"] = lab["why_full"].strip()
            ref["decode_v"] = 2
        if _norm_generativity(lab.get("generativity")):
            ref["generativity"] = _norm_generativity(lab.get("generativity"))
        if (lab.get("persona_trait") or "").strip():
            ref["persona_trait"] = lab["persona_trait"].strip()
        if (lab.get("primary_lever") or "").strip():
            ref["primary_lever"] = lab["primary_lever"].strip()
        changed.append({"ref_id": ref.get("ref_id"), "had_note": bool(op_note), "why": why})
    if changed:
        path = profiles.corpus_path(pid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in refs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return {"relabeled": changed}


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
                                   f"operator note: would have been a {claim}; promoted into the corpus", vpid,
                                   op_note=g.get("notes"))
                    if rid:
                        endorsed.append(rid)
                        if c.get("anchor_ref"):   # amplify the format that produced the endorsed line
                            attribute.credit_verdict({"anchor_refs": [c["anchor_ref"]]}, "keep", vpid)
    notes_by_reel = {r.get("reel_id"): (r.get("grade") or {}).get("notes")
                     for r in reel_store.graded(pid)}
    for g in all_grades:
        if g.get("type") == "authored" and (g.get("claim") or 0) >= min_rating:
            rid = _add_ref(g.get("caption") or "", g.get("claim") or 0, [], "operator_authored",
                           f"operator-AUTHORED in a grading note (claimed {g.get('claim')}/10) — "
                           "ground-truth voice", pid,
                           op_note=notes_by_reel.get((g.get("context") or {}).get("reel_id")))
            if rid:
                authored.append(rid)
    return {"posted_promoted": len(posted), "endorsed_promoted": len(endorsed),
            "authored_promoted": len(authored), "ref_ids": posted + endorsed + authored}
