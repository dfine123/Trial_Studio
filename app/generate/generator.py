"""Reel generator — the default (zero-input) spine.

CAPTION-FIRST (the caption is the post / the joke):
  profile audio -> Caption Engine (voice / serious lanes) -> rank clips that REACT to the caption
  -> beat slot plan -> fill slots with the caption-matched clips -> caption PNG -> compositor.

The caption leads; clips are chosen to play behind it (`_match_clips_to_caption`); the audio beat
map drives the cut timing. The reverse direction (caption reacting to a fixed clip — the
clip-aware lane + `_clip_context`) is wired but reserved for a later single-clip "reaction" mode.

`generate_reel` resolves clip source files from an explicit `sources` map, or for local dev by
matching indexed clips to sample files by duration (`resolve_local_sources`).
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid

from sqlalchemy import select

from app.audio import profile
from app.caption.llm import complete_json
from app.db import SessionLocal
from app.generate.sequencer import build_slot_plan, select_segments
from app.models import Clip, Segment
from app.render.caption_image import render_caption_png
from app.render.compositor import compose_reel


def _probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def resolve_local_sources(
    clip_durations: dict[str, float], samples_dir: str = "samples", max_diff: float = 0.5
) -> dict[str, str]:
    """Match each indexed clip to its local source file by duration (dev convenience).

    Refuses a match further than max_diff seconds away — so a missing source fails loudly
    instead of silently grabbing the closest-duration unrelated clip.
    """
    files = [
        os.path.join(samples_dir, f)
        for f in os.listdir(samples_dir)
        if f.lower().endswith((".mov", ".mp4"))
    ]
    sample_durs = [(p, _probe_duration(p)) for p in files]
    mapping, used = {}, set()
    for cid, dur in clip_durations.items():
        best, best_diff = None, 1e9
        for p, sd in sample_durs:
            if p in used:
                continue
            diff = abs(sd - (dur or 0.0))
            if diff < best_diff:
                best, best_diff = p, diff
        if best is None or best_diff > max_diff:
            raise RuntimeError(
                f"no local source within {max_diff}s for clip (duration {dur}); "
                f"closest was {best_diff:.2f}s off. Put the real clip in {samples_dir}/."
            )
        mapping[cid] = best
        used.add(best)
    return mapping


def _resolve_sources(chosen: list[dict], clip_dur: dict[str, float]) -> dict[str, str]:
    """Map each chosen clip_id to a local source file. Uses the clip's stored local path
    (r2_key) when it's an existing file (local-ingest mode), else falls back to matching a
    sample file by duration."""
    out: dict[str, str] = {}
    need: dict[str, float] = {}
    with SessionLocal() as s:
        for cid in {c["clip_id"] for c in chosen}:
            clip = s.get(Clip, uuid.UUID(cid))
            if clip and clip.r2_key and os.path.exists(clip.r2_key):
                out[cid] = clip.r2_key
            else:
                need[cid] = clip_dur.get(cid)
    if need:
        out.update(resolve_local_sources(need))
    return out


def _load_segments(clip_ids: list[str] | None = None):
    """Return (segments, clip_durations, clip_meta) for indexed clips (optionally filtered)."""
    with SessionLocal() as s:
        q = (
            select(Segment, Clip)
            .join(Clip, Segment.clip_id == Clip.id)
            .where(Clip.status == "indexed")
        )
        if clip_ids:
            q = q.where(Clip.id.in_(clip_ids))
        rows = s.execute(q).all()
    segs, clip_dur, clip_meta = [], {}, {}
    for seg, clip in rows:
        cid = str(clip.id)
        segs.append({
            "id": str(seg.id), "clip_id": cid,
            "start_ts": seg.start_ts, "end_ts": seg.end_ts, "duration": seg.duration,
            "usability_score": seg.usability_score, "energy": seg.energy,
            "is_hero": seg.is_hero, "vibe_tags": clip.vibe_tags or [],
        })
        clip_dur[cid] = clip.duration
        clip_meta[cid] = {
            "summary": clip.summary, "setting": clip.setting,
            "vibe_tags": clip.vibe_tags or [], "time_of_day": clip.time_of_day,
            "camera_movement": clip.camera_movement,
        }
    return segs, clip_dur, clip_meta


# Audio vibes that call for a reflective / serious caption rather than the funny voice.
_SERIOUS_VIBES = {"reflective", "wisdom", "hard-truth", "introspective",
                  "business-realtalk", "building", "hindsight", "growth", "late-night"}


def _pick_reel_caption(cands: list[dict], prefer: str = "voice") -> dict | None:
    """Pick one caption for the reel. `prefer` ('voice'|'serious') sets which lane wins —
    reflective/serious audios prefer the serious lane, everything else the funny voice. The
    clip-aware lane is a last resort (it assumes a fixed clip, absent in caption-first)."""
    if not cands:
        return None
    order = ["serious", "voice", "clip"] if prefer == "serious" else ["voice", "serious", "clip"]
    for lane in order:
        for c in cands:
            if c.get("lane") == lane and (c.get("text") or "").strip():
                return c
    return cands[0]


_MATCH_SYS = """You match flashy b-roll CLIPS to a CAPTION for a 9:16 reel. The caption is the post (the joke people read); the clips play BEHIND it as backdrop. Rank the clips by how well each FITS behind THIS caption — a clip fits if its scene / subject / energy reinforces or playfully plays off the caption. Generic flashy footage is a weak-but-acceptable fallback; an on-point scene is best.

Return ONLY JSON, no prose: {"ranked": [clip indices, best-fit FIRST, every index included]}"""


def _match_clips_to_caption(caption_text: str, clip_meta: dict, max_clips: int = 40) -> list[str]:
    """Rank clip_ids by how well each fits behind the caption (clips react to the caption)."""
    items = list(clip_meta.items())
    if len(items) <= 1:
        return [cid for cid, _ in items]
    items = items[:max_clips]
    lines = []
    for i, (_cid, m) in enumerate(items):
        summ = (m.get("summary") or "").strip().replace("\n", " ")[:160]
        vibe = ", ".join((m.get("vibe_tags") or [])[:6])
        lines.append(f"[{i}] {summ}  | vibe: {vibe}")
    user = f"CAPTION:\n{caption_text}\n\nCLIPS:\n" + "\n".join(lines)
    try:
        out = complete_json(_MATCH_SYS, user, effort="low", max_tokens=600)
        start, end = out.find("{"), out.rfind("}")
        order = json.loads(out[start:end + 1]).get("ranked", []) if start != -1 else []
        ranked = [items[i][0] for i in order if isinstance(i, int) and 0 <= i < len(items)]
    except Exception:  # noqa: BLE001 — matching is best-effort; fall back to usability order
        ranked = []
    seen = set(ranked)
    ranked += [cid for cid, _ in items if cid not in seen]
    return ranked


def generate_reel(
    audio_path: str,
    niche: str,
    out_path: str,
    *,
    audio_desc: str | None = None,
    audio_bpm: float | None = None,
    audio_energy: str | None = None,
    audio_vibe: list[str] | None = None,
    caption_text: str | None = None,
    caption_vibe: list[str] | None = None,
    sources: dict[str, str] | None = None,
    clip_ids: list[str] | None = None,
    work_png: str = "tmp/reel_caption.png",
) -> dict:
    bp = profile.analyze(audio_path)
    slots = build_slot_plan(bp.beat_map, bp.duration)
    reel_dur = slots[-1].end

    segs, clip_dur, clip_meta = _load_segments(clip_ids=clip_ids)
    if not segs:
        raise RuntimeError("no indexed segments available to build a reel")

    # CAPTION FIRST — the caption is the post (a standalone joke).
    if caption_text is None:
        from app.caption.engine import generate as gen_caps  # lazy: pulls anthropic + corpus

        bpm = audio_bpm or bp.bpm
        energy = audio_energy or ("low" if bpm and bpm < 100 else "high" if bpm and bpm > 132 else "mid")
        parts = []
        if niche and niche.strip():
            parts.append(niche.strip())
        if audio_desc:
            parts.append(f"audio: {audio_desc}")
        if audio_vibe:
            parts.append("vibe lean: " + ", ".join(audio_vibe))
        note = "; ".join(parts) or None
        cands = gen_caps(audio_energy=energy, notes=note, n=6) or []
        prefer = "serious" if (energy == "low" or (audio_vibe and set(audio_vibe) & _SERIOUS_VIBES)) else "voice"
        pick = _pick_reel_caption(cands, prefer)
        caption_text = (pick.get("text") if pick else None) or "no caption"

    # Clips REACT to the caption — rank by fit, prefer the best behind this caption.
    ranked = _match_clips_to_caption(caption_text, clip_meta)
    preferred = set(ranked[: max(3, len(ranked) // 2)])
    chosen = select_segments(slots, segs, caption_vibe_tags=caption_vibe, preferred_clip_ids=preferred)

    if sources is None:
        sources = _resolve_sources(chosen, clip_dur)

    shots = [
        {"src_path": sources[c["clip_id"]], "src_start": c["src_start"], "duration": c["slot_dur"]}
        for c in chosen
    ]

    render_caption_png(caption_text, work_png)
    compose_reel(shots, work_png, audio_path, out_path, reel_dur)

    return {"output": out_path, "caption": caption_text, "matched_clips": ranked[:3],
            "duration": round(reel_dur, 2), "shots": len(shots), "sequence": chosen}
