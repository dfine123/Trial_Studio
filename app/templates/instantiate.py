"""Apply a Template to a creator's clips -> a rendered multi-segment reel.

Orchestrates the leaves: match clips by the author's clip-type (from existing indexing), regenerate
the captions under each slot's variability rules, render per-segment caption PNGs, and compose the
multi-segment reel. Aborts with a clear message if the creator's library can't fill a segment.
"""
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Clip
from app.render.caption_image import render_caption_png
from app.render.compositor import compose_template_reel
from app.templates.arc import regenerate_captions
from app.templates.interpret import interpret_template
from app.templates.match import match_clips

_log = logging.getLogger(__name__)


def creator_clips() -> list[dict]:
    """The creator's indexed clips as match-ready digests (read from the existing indexing)."""
    with SessionLocal() as s:
        rows = s.scalars(select(Clip).where(Clip.status == "indexed")).all()
        return [{"id": str(c.id), "summary": c.summary, "setting": c.setting,
                 "vibe": c.vibe_tags or [], "src": c.r2_key, "duration": c.duration} for c in rows]


def _resolve_src(clip: dict | None) -> str | None:
    src = (clip or {}).get("src")
    return src if (src and os.path.exists(src)) else None


_USAGE_PATH = "var/template_clip_usage.json"


def _load_usage() -> dict:
    try:
        with open(_USAGE_PATH) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _log_usage(clip_ids: list[str]) -> None:
    u = _load_usage()
    for cid in clip_ids:
        u[cid] = u.get(cid, 0) + 1
    os.makedirs("var", exist_ok=True)
    tmp = _USAGE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(u, fh)
    os.replace(tmp, _USAGE_PATH)


def _probe_dur(path: str) -> float:
    """Exact playable duration of a video file (so we know when a clip is shorter than its slot)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _pick_filler(clips: list[dict], by_id: dict, used: set, alternate_ids: list, prim_vibe: set) -> dict | None:
    """Next fresh clip to fill a gap inside a segment: the matcher's ranked ALTERNATES first (best
    same-kind fit), then the most vibe-similar still-unused clip. None if nothing fresh is left."""
    for fid in alternate_ids:
        if fid not in used and fid in by_id:
            return by_id[fid]
    cand = [c for c in clips if c["id"] not in used]
    if not cand:
        return None
    cand.sort(key=lambda c: len(prim_vibe & set(c.get("vibe") or [])), reverse=True)
    return cand[0]


def _plan_timeline(segments, assign, alternates, by_id, clips, dur_of, resolve_src, min_fill=1.2):
    """Pure timeline planner (unit-testable — ffprobe/file access injected via dur_of & resolve_src).

    Each authored segment must hold its slot so the NEXT caption lands on its beat — EXCEPT the last,
    which may end early. Per segment: trim the primary clip to its real length (no freeze); fill a
    leftover >= min_fill with fresh fitting clips; freeze-hold a tiny mid-reel leftover (keeps the
    beat); leave a last-segment leftover unfilled (the reel ends early, audio trims to match).

    Returns (video_chunks=[{src_path,src_start,duration}], spans=[(t_in,t_out) per segment], total)."""
    used = {v for v in assign.values() if v}
    video_chunks: list[dict] = []
    spans: list[tuple] = []
    cursor = 0.0
    last = len(segments) - 1
    for idx, s in enumerate(segments):
        slot = float(s["t_out"]) - float(s["t_in"])
        seg_start = cursor
        if slot <= 0:                                        # malformed/zero slot -> contributes no time
            spans.append((cursor, cursor))
            continue
        prim = by_id.get(assign.get(str(s["index"])))
        src = resolve_src(prim)
        if src is None:
            raise RuntimeError(
                f"segment {s['index']}: no usable clip (source_type="
                f"{s.get('source_type', 'creator_clip')!r} may be unsupported, or the clip file is missing)")
        pdur = dur_of(src)
        if pdur > 0.1:
            take = min(pdur, slot)
        elif idx == last:
            take = slot                                      # last segment may run long; filling it is fine
        else:                                                # unknown length, non-last: don't trust the slot
            take = min(slot, min_fill)                       # -> let the fill loop cover the rest with footage
            _log.warning("ffprobe could not read %s; planning conservatively to avoid a frozen tail", src)
        seg_chunks = [{"src_path": src, "src_start": 0.0, "duration": take}]
        remaining = slot - take
        prim_vibe = set((prim or {}).get("vibe") or [])
        while remaining >= min_fill:                         # fill a big gap with fresh, fitting footage
            f = _pick_filler(clips, by_id, used, alternates.get(str(s["index"])) or [], prim_vibe)
            if not f:
                break
            used.add(f["id"])
            fsrc = resolve_src(f)
            if not fsrc:
                continue
            fdur = dur_of(fsrc)
            ftake = min(fdur, remaining) if fdur > 0.1 else remaining
            if ftake < 0.4:                                 # too short to be worth a cut
                continue
            seg_chunks.append({"src_path": fsrc, "src_start": 0.0, "duration": ftake})
            remaining -= ftake
        if remaining > 1e-3 and idx != last:                # ANY mid-reel leftover -> hold, keep next on beat
            seg_chunks[-1]["duration"] += remaining
        seg_content = sum(c["duration"] for c in seg_chunks)
        video_chunks.extend(seg_chunks)
        spans.append((seg_start, seg_start + seg_content))
        cursor = seg_start + seg_content
    return video_chunks, spans, cursor


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
    random.shuffle(clips)                                   # vary the order the matcher sees each run
    usage = _load_usage()
    recent = [cid for cid, _ in sorted(usage.items(), key=lambda kv: -kv[1])][:8]   # most-used -> deprioritize

    # 1. match the creator's clips to the segment clip-types (honors authored fallbacks; may abort)
    seg_for_match = [{"index": s["index"], "clip_type": (s.get("clip_criteria") or {}).get("clip_type")}
                     for s in segments if s.get("source_type", "creator_clip") == "creator_clip"]
    if not seg_for_match:
        raise RuntimeError("this template has no creator-clip segments to fill")
    m = match_clips(seg_for_match, clips, recent=recent)
    if not m.get("ok"):
        raise RuntimeError("can't apply this template to this creator — "
                           + (m.get("warning") or "a segment can't be filled by these clips"))
    assign = {str(k): v for k, v in m["assignments"].items()}
    _log_usage([v for v in assign.values() if v])           # spread footage usage across generations
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

    # 3. plan the video timeline (trim-vs-fill, tail-trim) then render captions over the rebuilt spans
    os.makedirs("tmp", exist_ok=True)
    alternates = m.get("alternates", {}) or {}
    video_chunks, spans, total = _plan_timeline(
        segments, assign, alternates, by_id, clips, _probe_dur, _resolve_src)
    if not video_chunks:
        raise RuntimeError("template produced no video (all segments were empty or malformed)")
    caption_windows = []
    for (t_in, t_out), s in zip(spans, segments):
        sid = s.get("caption_slot_id")
        cap = (captions.get(sid) if sid else None) or ""
        cap_png = None
        if cap.strip():
            cap_png = os.path.abspath(os.path.join("tmp", f"tpl_cap_{uuid.uuid4().hex}.png"))
            render_caption_png(cap, cap_png)
        caption_windows.append({"caption_png": cap_png, "t_in": t_in, "t_out": t_out})

    compose_template_reel(video_chunks, caption_windows, audio_path, out_path, total)
    return {"output": out_path, "captions": captions, "assignments": assign,
            "segments": len(segments), "duration": round(total, 2)}
