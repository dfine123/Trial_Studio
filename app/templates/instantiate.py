"""Apply a Template to a creator's clips -> a rendered multi-segment reel.

Orchestrates the leaves: match clips by the author's clip-type (from existing indexing), regenerate
the captions under each slot's variability rules, render per-segment caption PNGs, and compose the
multi-segment reel. Aborts with a clear message if the creator's library can't fill a segment.
"""
from __future__ import annotations

import os
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Clip
from app.render.caption_image import render_caption_png
from app.render.compositor import compose_template_reel
from app.templates.arc import regenerate_captions
from app.templates.interpret import interpret_template
from app.templates.match import match_clips


def creator_clips() -> list[dict]:
    """The creator's indexed clips as match-ready digests (read from the existing indexing)."""
    with SessionLocal() as s:
        rows = s.scalars(select(Clip).where(Clip.status == "indexed")).all()
        return [{"id": str(c.id), "summary": c.summary, "setting": c.setting,
                 "vibe": c.vibe_tags or [], "src": c.r2_key, "duration": c.duration} for c in rows]


def _resolve_src(clip: dict | None) -> str | None:
    src = (clip or {}).get("src")
    return src if (src and os.path.exists(src)) else None


def instantiate_template(spec: dict, audio_path: str, out_path: str, clips: list[dict] | None = None) -> dict:
    """Render a reel by applying `spec` (a TemplateSpec dict) to a creator's clips."""
    formula = spec.get("formula") or {}
    if not formula.get("slots"):
        formula = interpret_template(spec)        # enrich on the fly if it was never read
    segments = sorted(spec.get("segments", []), key=lambda s: s.get("index", 0))
    if not segments:
        raise RuntimeError("template has no segments")
    slots = {c["id"]: c for c in spec.get("caption_slots", [])}
    if clips is None:
        clips = creator_clips()

    # 1. match the creator's clips to the segment clip-types (honors authored fallbacks; may abort)
    seg_for_match = [{"index": s["index"], "clip_type": (s.get("clip_criteria") or {}).get("clip_type")}
                     for s in segments if s.get("source_type", "creator_clip") == "creator_clip"]
    m = match_clips(seg_for_match, clips)
    if not m.get("ok"):
        raise RuntimeError("can't apply this template to this creator — "
                           + (m.get("warning") or "a segment can't be filled by these clips"))
    assign = {str(k): v for k, v in m["assignments"].items()}
    by_id = {c["id"]: c for c in clips}

    # 2. regenerate the captions under the variability rules
    regen = []
    for s in segments:
        sid = s.get("caption_slot_id")
        if not sid:
            continue
        c = by_id.get(assign.get(str(s["index"])))
        regen.append({"index": s["index"], "slot_id": sid, "exemplar": (slots.get(sid) or {}).get("exemplar"),
                      "clip_summary": (c or {}).get("summary"), "clip_vibe": (c or {}).get("vibe")})
    captions = regenerate_captions(formula, regen)

    # 3. resolve sources, render per-segment caption PNGs, build the compositor bindings
    os.makedirs("tmp", exist_ok=True)
    bindings = []
    for s in segments:
        c = by_id.get(assign.get(str(s["index"])))
        src = _resolve_src(c)
        if src is None:
            raise RuntimeError(f"segment {s['index']}: matched clip's source file is missing")
        sid = s.get("caption_slot_id")
        cap = (captions.get(sid) if sid else None) or ""
        cap_png = None
        if cap.strip():
            cap_png = os.path.abspath(os.path.join("tmp", f"tpl_cap_{uuid.uuid4().hex}.png"))
            render_caption_png(cap, cap_png)
        bindings.append({"src_path": src, "src_start": 0.0, "duration": s["t_out"] - s["t_in"],
                         "t_in": s["t_in"], "t_out": s["t_out"], "caption_png": cap_png})

    total = segments[-1]["t_out"]
    compose_template_reel(bindings, audio_path, out_path, total)
    return {"output": out_path, "captions": captions, "assignments": assign,
            "segments": len(segments), "duration": round(total, 2)}
