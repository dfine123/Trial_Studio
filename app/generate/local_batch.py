"""Local battle-test harness — index a folder of clips SYNCHRONOUSLY (no R2, no RQ queue) and
generate reels from them.

Each clip's absolute local path is stored in `r2_key` so `generate_reel` can resolve sources
without R2 (local mode). This exercises the real work — QC, segmentation, Twelve Labs, OpenCV,
the caption engine, and ffmpeg compositing — while skipping the deploy plumbing (object storage
+ async queue), which is tested separately.
"""
from __future__ import annotations

import os
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.generate.generator import generate_reel
from app.indexing.pipeline import run_pipeline
from app.models import Audio, Clip, User

_VID_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm")


def _default_user_id() -> uuid.UUID:
    with SessionLocal() as s:
        u = s.scalar(select(User).order_by(User.created_at).limit(1))
        if u is None:
            u = User(handle="default", description="default V1 user")
            s.add(u)
            s.commit()
            s.refresh(u)
        return u.id


def ingest_folder(
    folder: str, limit: int | None = None, max_indexed: int | None = None
) -> list[dict]:
    """Index videos in `folder` synchronously. `limit` caps files attempted; `max_indexed` stops
    once that many clips successfully index (QC rejects don't count). One record per file."""
    user_id = _default_user_id()
    out: list[dict] = []
    indexed = 0
    # Skip files already indexed — makes the ingest resumable across runs / WSL reaps.
    with SessionLocal() as s:
        done = {c.r2_key for c in s.scalars(
            select(Clip).where(Clip.status.in_(["indexed", "indexing", "rejected"]))).all() if c.r2_key}
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(_VID_EXT))
    if limit:
        files = files[:limit]
    for fname in files:
        path = os.path.abspath(os.path.join(folder, fname))
        if path in done:
            out.append({"file": fname, "clip_id": None, "status": "skipped",
                        "segments": 0, "reason": "already indexed"})
            continue
        clip_id = uuid.uuid4()
        with SessionLocal() as s:
            s.add(Clip(id=clip_id, user_id=user_id, r2_key=path, status="uploaded"))
            s.commit()
        reason = None
        try:
            status = run_pipeline(clip_id, source_path=path)
        except Exception as exc:  # noqa: BLE001 — record per-file, never abort the batch
            status = "error"
            reason = str(exc)[:300]
        with SessionLocal() as s:
            c = s.get(Clip, clip_id)
            if c is not None:
                reason = reason or c.rejection_reason
                nseg = len(c.segments)
            else:
                nseg = 0
        out.append({"file": fname, "clip_id": str(clip_id), "status": status,
                    "segments": nseg, "reason": reason})
        if status == "indexed":
            indexed += 1
            if max_indexed and indexed >= max_indexed:
                break
    return out


def local_sources_map(clip_ids: list[str] | None = None) -> dict[str, str]:
    """clip_id -> local path, for indexed clips whose r2_key is an existing local file."""
    with SessionLocal() as s:
        q = select(Clip).where(Clip.status == "indexed")
        if clip_ids:
            q = q.where(Clip.id.in_(clip_ids))
        clips = s.scalars(q).all()
    return {str(c.id): c.r2_key for c in clips if c.r2_key and os.path.exists(c.r2_key)}


def generate_reels(
    clip_ids: list[str], n: int = 3, out_dir: str = "var/reels", niche: str = ""
) -> list[dict]:
    """Generate `n` reels from the given local clips, cycling through the seeded audios."""
    os.makedirs(out_dir, exist_ok=True)
    sources = local_sources_map(clip_ids)
    if not sources:
        raise RuntimeError("no local sources for the given clips (are they indexed + present?)")
    with SessionLocal() as s:
        audios = s.scalars(select(Audio).order_by(Audio.created_at)).all()
    if not audios:
        raise RuntimeError("no audios seeded — run app.seed.seed_audio")

    results: list[dict] = []
    for i in range(n):
        audio = audios[i % len(audios)]
        audio_path = os.path.join("samples", "audio", os.path.basename(audio.r2_key or ""))
        print(f"[reel {i + 1}/{n}] audio={audio.description[:34]!r} ...", flush=True)
        if not os.path.exists(audio_path):
            r = {"error": f"audio file missing: {audio_path}", "audio": audio.description}
            results.append(r)
            print(f"    SKIP: {r['error']}", flush=True)
            continue
        out = os.path.join(out_dir, f"battle_{i + 1}_{uuid.uuid4().hex[:8]}.mp4")
        try:
            res = generate_reel(
                audio_path, niche, out,
                audio_desc=audio.description, audio_bpm=audio.bpm,
                audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags,
                sources=sources, clip_ids=clip_ids,
            )
            r = {"reel": out, "audio": audio.description, "caption": res["caption"],
                 "shots": res["shots"], "duration": res["duration"]}
            results.append(r)
            print(f"    OK {out} [{r['duration']}s, {r['shots']} shots]\n    caption: {r['caption']!r}", flush=True)
        except Exception as exc:  # noqa: BLE001
            results.append({"error": str(exc)[:300], "audio": audio.description})
            print(f"    ERROR: {str(exc)[:200]}", flush=True)
    return results
