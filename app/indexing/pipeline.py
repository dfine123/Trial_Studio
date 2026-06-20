"""Indexing orchestrator (called by the RQ worker).

Flow: resolve source (R2 download or a direct path) -> QC gate -> PySceneDetect + long-take
windowing -> Twelve Labs (index/poll/Pegasus/Marengo) -> OpenCV per-segment metrics ->
assemble + persist Clip fields + Segment rows -> status=indexed.

Failures (QC reject, TL failure, errors) set status=rejected with a human-readable reason.
"""
from __future__ import annotations

import os
import statistics
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal
from app.indexing import qc, segmentation, twelvelabs, visual
from app.models import Clip, Segment


def _coerce_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1"}
    return None


def _map_description(window, moments) -> str | None:
    best, best_ov = None, 0.0
    for m in moments:
        try:
            s, e = float(m.get("start", 0)), float(m.get("end", 0))
        except (TypeError, ValueError):
            continue
        ov = max(0.0, min(window.end_ts, e) - max(window.start_ts, s))
        if ov > best_ov:
            best_ov, best = ov, m
    if best is None and moments:
        best = moments[0]  # fall back to first moment
    return (best or {}).get("description")


def assemble(windows, visuals, analysis: dict, embedding):
    """Pure merge of windowing + OpenCV metrics + Twelve Labs analysis. Testable in isolation."""
    moments = analysis.get("moments") or []
    n = len(windows)
    usab = [v.usability_score for v in visuals]
    k = max(1, n // 5)  # flag top ~20% (>=1) as hero
    hero_idx = set(sorted(range(n), key=lambda i: usab[i], reverse=True)[:k]) if n else set()

    seg_rows = []
    for i, (w, v) in enumerate(zip(windows, visuals)):
        seg_rows.append(
            dict(
                idx=i,
                start_ts=round(w.start_ts, 3),
                end_ts=round(w.end_ts, 3),
                duration=round(w.duration, 3),
                description=_map_description(w, moments),
                motion_intensity=v.motion_intensity,
                energy=v.energy,
                shot_scale=v.shot_scale,
                lighting=v.lighting,
                luminance=v.avg_luminance,
                color_temp_k=v.color_temp_k,
                subject_bbox=v.subject_bbox,
                usability_score=v.usability_score,
                is_hero=(i in hero_idx),
            )
        )

    avg_lum = round(statistics.fmean([v.avg_luminance for v in visuals]), 4) if visuals else None
    cct = round(statistics.fmean([v.color_temp_k for v in visuals]), 1) if visuals else None
    palette = None
    if hero_idx:
        hi = max(hero_idx, key=lambda i: usab[i])
        palette = visuals[hi].dominant_palette

    clip_fields = dict(
        summary=analysis.get("summary"),
        setting=analysis.get("setting"),
        lighting_tags=analysis.get("lighting_tags"),
        vibe_tags=analysis.get("vibe_tags"),
        time_of_day=analysis.get("time_of_day"),
        camera_movement=analysis.get("camera_movement"),
        has_speech=_coerce_bool(analysis.get("has_speech")),
        has_music=_coerce_bool(analysis.get("has_music")),
        avg_luminance=avg_lum,
        color_temp_k=cct,
        dominant_palette=palette,
        embedding=embedding,
        quality_flags=[],
    )
    return clip_fields, seg_rows


def _resolve_source(clip: Clip, source_path: str | None) -> tuple[str, bool]:
    """Return (local_path, is_temp). Downloads from R2 unless a direct path is given."""
    if source_path:
        return source_path, False
    from app.storage import r2  # imported here so QC/segment tests don't require R2 config

    os.makedirs(settings.work_dir, exist_ok=True)
    local = os.path.join(settings.work_dir, f"{clip.id}_source")
    r2.download_to_path(clip.r2_key, local)
    return local, True


def run_pipeline(clip_id: str, source_path: str | None = None) -> str:
    """Index one clip. Returns the final status. Marks rejected (with reason) on any failure."""
    if isinstance(clip_id, str):
        clip_id = uuid.UUID(clip_id)
    session = SessionLocal()
    path, is_temp = None, False
    try:
        clip = session.get(Clip, clip_id)
        if clip is None:
            return "missing"

        clip.status = "indexing"
        session.commit()

        path, is_temp = _resolve_source(clip, source_path)

        # QC — record dims either way
        qcres = qc.check(path, settings.min_resolution, settings.min_fps)
        p = qcres.probe
        clip.width, clip.height, clip.fps = p.width, p.height, p.fps
        clip.duration, clip.bitrate = p.duration, p.bitrate
        if not qcres.passed:
            clip.status = "rejected"
            clip.rejection_reason = qcres.reason
            session.commit()
            return clip.status

        # Segmentation (+ long-take windowing)
        windows = segmentation.segment_video(path, total_duration=p.duration)

        # Twelve Labs (index -> poll -> Pegasus -> Marengo)
        c = twelvelabs.client()
        index_id = twelvelabs.ensure_index(c)
        task = twelvelabs.index_video(c, index_id, video_file=path)
        clip.twelvelabs_video_id = task.video_id
        session.commit()

        analysis = twelvelabs.analyze_clip(c, task.video_id)
        embedding = None
        if settings.enable_marengo_embedding:
            try:
                embedding = twelvelabs.embed_video(c, video_file=path)
            except Exception:  # noqa: BLE001 — embedding is best-effort (V1)
                embedding = None

        # OpenCV per-segment metrics
        visuals = [visual.analyze_segment(path, w.start_ts, w.end_ts) for w in windows]

        # Assemble + persist
        clip_fields, seg_rows = assemble(windows, visuals, analysis, embedding)
        for key, val in clip_fields.items():
            setattr(clip, key, val)
        session.execute(delete(Segment).where(Segment.clip_id == clip.id))
        for sr in seg_rows:
            session.add(Segment(clip_id=clip.id, **sr))

        clip.status = "indexed"
        clip.indexed_at = datetime.now(timezone.utc)
        session.commit()
        return clip.status

    except Exception as exc:  # noqa: BLE001 — record the failure on the clip per spec gotcha #4
        session.rollback()
        clip = session.get(Clip, clip_id)
        if clip is not None:
            clip.status = "rejected"
            clip.rejection_reason = f"indexing error: {exc}"[:1000]
            session.commit()
        raise
    finally:
        if is_temp and path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        session.close()
