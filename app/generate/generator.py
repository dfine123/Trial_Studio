"""Reel generator — the default (zero-input) spine.

profile -> audio (beat map) -> Caption Assistant -> beat slot plan -> best-segment selection
-> caption PNG -> compositor -> 9:16 reel.

`generate_reel` resolves clip source files. In production it downloads from R2 by the clip's
r2_key; for local dev it can match indexed clips to the sample files by duration (resolve via
the `sources` arg or `resolve_local_sources`).
"""
from __future__ import annotations

import os
import subprocess

from sqlalchemy import select

from app.audio import profile
from app.caption.assistant import generate_captions
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


def _load_segments():
    with SessionLocal() as s:
        rows = s.execute(
            select(Segment, Clip).join(Clip, Segment.clip_id == Clip.id).where(Clip.status == "indexed")
        ).all()
    segs, clip_dur = [], {}
    for seg, clip in rows:
        segs.append({
            "id": str(seg.id), "clip_id": str(seg.clip_id),
            "start_ts": seg.start_ts, "end_ts": seg.end_ts, "duration": seg.duration,
            "usability_score": seg.usability_score, "energy": seg.energy,
            "is_hero": seg.is_hero, "vibe_tags": clip.vibe_tags or [],
        })
        clip_dur[str(clip.id)] = clip.duration
    return segs, clip_dur


def generate_reel(
    audio_path: str,
    niche: str,
    out_path: str,
    caption_text: str | None = None,
    caption_vibe: list[str] | None = None,
    sources: dict[str, str] | None = None,
    work_png: str = "tmp/reel_caption.png",
) -> dict:
    bp = profile.analyze(audio_path)

    if caption_text is None:
        caps = generate_captions(f"audio: {os.path.basename(audio_path)}", niche, n=3)
        caption_text = caps[0]["text"] if caps else "no caption"
        caption_vibe = caps[0].get("vibe_tags") if caps else []

    slots = build_slot_plan(bp.beat_map, bp.duration)
    reel_dur = slots[-1].end

    segs, clip_dur = _load_segments()
    chosen = select_segments(slots, segs, caption_vibe_tags=caption_vibe)

    if sources is None:
        sources = resolve_local_sources({c["clip_id"]: clip_dur.get(c["clip_id"]) for c in chosen})

    shots = [
        {"src_path": sources[c["clip_id"]], "src_start": c["src_start"], "duration": c["slot_dur"]}
        for c in chosen
    ]

    render_caption_png(caption_text, work_png)
    compose_reel(shots, work_png, audio_path, out_path, reel_dur)

    return {"output": out_path, "caption": caption_text, "duration": round(reel_dur, 2),
            "shots": len(shots), "sequence": chosen}
