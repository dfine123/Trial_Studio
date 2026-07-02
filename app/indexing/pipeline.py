"""Indexing orchestrator (called by the RQ worker).

Flow: resolve source (R2 download or a direct path) -> QC gate -> PySceneDetect + long-take
windowing -> Twelve Labs (index/poll/Pegasus/Marengo) -> OpenCV per-segment metrics ->
assemble + persist Clip fields + Segment rows -> status=indexed.

Failures (QC reject, TL failure, errors) set status=rejected with a human-readable reason.
"""
from __future__ import annotations

import os
import statistics
import subprocess
import threading
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


def _pad_short_for_tl(src: str, real_duration: float, work_dir: str, target: float) -> str:
    """Freeze-pad a sub-minimum clip up to `target`s so Twelve Labs accepts it.

    Real frames keep their real speed and timestamps; only a frozen hold of the last frame
    is appended (and later ignored, since it falls outside the original clip's segments).
    Returns the temp path of the padded copy (caller deletes it).
    """
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, f"tlpad_{uuid.uuid4().hex}.mp4")
    pad = max(0.1, target - (real_duration or 0.0))
    base = [
        "ffmpeg", "-y", "-i", src,
        "-vf", f"tpad=stop_mode=clone:stop_duration={pad:.2f}",
        "-t", f"{target:.2f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
    ]
    try:
        subprocess.run(base + ["-af", "apad", "-c:a", "aac", out], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(base + ["-an", out], check=True, capture_output=True)  # no audio stream
    return out


# OpenCV decode is the memory-heavy part — serialize ONLY it, so several clips can be in flight
# (overlapping their long TwelveLabs remote waits) while at most one decodes locally at a time.
_CV2 = threading.Semaphore(1)

INDEX_TRACE: list[str] = []  # diagnostic ring of recent [idx] trace lines (read by /api/debug/index-test)


def _t(msg: str, flush: bool = True) -> None:
    """Trace: append to the in-memory ring AND print to stdout (Railway logs)."""
    INDEX_TRACE.append(msg)
    del INDEX_TRACE[:-400]
    print(msg, flush=flush)


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
        _t(f"[idx] {clip_id} run_pipeline entered (status=indexing)", flush=True)

        path, is_temp = _resolve_source(clip, source_path)
        _t(f"[idx] {clip_id} source resolved: {path}", flush=True)

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

        # Segmentation (+ long-take windowing) — cv2-heavy, one at a time
        _t(f"[idx] {clip_id} qc passed {p.width}x{p.height} {p.fps}fps {p.duration}s — segmenting…", flush=True)
        with _CV2:
            windows = segmentation.segment_video(path, total_duration=p.duration)
        _t(f"[idx] {clip_id} segmented into {len(windows)} window(s)", flush=True)

        # Twelve Labs (index -> poll -> Pegasus -> Marengo).
        # Sub-4s clips: index a freeze-padded copy (TL's minimum) but keep the original as the
        # real asset; the analysis is attributed to the original.
        _t(f"[idx] {clip_id} building TwelveLabs client + index…", flush=True)
        c = twelvelabs.client()
        index_id = twelvelabs.ensure_index(c)
        _t(f"[idx] {clip_id} TL client ready, index={index_id}", flush=True)
        tl_source, tl_padded, real_dur = path, False, None
        if p.duration and p.duration < settings.tl_min_duration:
            tl_source = _pad_short_for_tl(path, p.duration, settings.work_dir, settings.tl_pad_target)
            tl_padded, real_dur = True, p.duration
        try:
            _t(f"[idx] {clip_id} TL.index_video uploading {tl_source}…", flush=True)
            task = twelvelabs.index_video(c, index_id, video_file=tl_source)
            clip.twelvelabs_video_id = task.video_id
            session.commit()
            _t(f"[idx] {clip_id} TL indexed video_id={task.video_id} — analyzing…", flush=True)

            analysis = twelvelabs.analyze_clip(c, task.video_id, real_duration=real_dur)
            embedding = None
            if settings.enable_marengo_embedding:
                try:
                    embedding = twelvelabs.embed_video(c, video_file=tl_source)
                except Exception:  # noqa: BLE001 — embedding is best-effort (V1)
                    embedding = None
        finally:
            if tl_padded and os.path.exists(tl_source):
                try:
                    os.remove(tl_source)
                except OSError:
                    pass

        # OpenCV per-segment metrics — cv2-heavy, one at a time
        with _CV2:
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
        _t(f"[idx] {clip_id} DONE — indexed", flush=True)
        return clip.status

    except Exception as exc:  # noqa: BLE001 — record the failure on the clip per spec gotcha #4
        import traceback
        _t(f"[idx] {clip_id} ERROR: {exc}", flush=True)
        traceback.print_exc()
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
