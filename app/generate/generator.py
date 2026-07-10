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
import threading
import uuid

from sqlalchemy import select

from app.audio import profile
from app.caption.llm import complete_json
from app.db import SessionLocal
from app.generate.sequencer import build_slot_plan, select_segments
from app.models import Clip, Segment
from app.render.caption_image import render_caption_png
from app.render.compositor import compose_reel


_CLIP_USAGE_PATH = "var/clip_usage.json"
_USAGE_IO_LOCK = threading.Lock()   # parallel batch renders: guards the usage-file read-modify-write
                                    # AND the shared in-batch clip ledger


def _load_clip_usage() -> dict[str, int]:
    """Cumulative per-clip use count across reels — drives cross-reel footage variety."""
    try:
        with open(_CLIP_USAGE_PATH) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _log_clip_usage(clip_ids: list[str]) -> None:
    with _USAGE_IO_LOCK:   # concurrent renders were losing increments (read-modify-write race)
        usage = _load_clip_usage()
        for cid in clip_ids:
            usage[cid] = usage.get(cid, 0) + 1
        os.makedirs(os.path.dirname(_CLIP_USAGE_PATH) or ".", exist_ok=True)
        tmp = _CLIP_USAGE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(usage, fh)
        os.replace(tmp, _CLIP_USAGE_PATH)


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
    """Return (segments, clip_durations, clip_meta, clip_emb) for the ACTIVE profile's indexed clips.
    clip_emb (Marengo whole-video vectors) is kept SEPARATE from clip_meta so it never leaks into the
    caption-fit LLM prompt — it exists for VISUAL de-duplication at selection time."""
    from app import profiles
    with SessionLocal() as s:
        q = (
            select(Segment, Clip)
            .join(Clip, Segment.clip_id == Clip.id)
            .where(Clip.status == "indexed", Clip.user_id == profiles.active_id())
        )
        if clip_ids:
            q = q.where(Clip.id.in_(clip_ids))
        rows = s.execute(q).all()
    segs, clip_dur, clip_meta, clip_emb = [], {}, {}, {}
    for seg, clip in rows:
        cid = str(clip.id)
        segs.append({
            "id": str(seg.id), "clip_id": cid,
            "start_ts": seg.start_ts, "end_ts": seg.end_ts, "duration": seg.duration,
            "usability_score": seg.usability_score, "energy": seg.energy,
            "luminance": seg.luminance,
            "is_hero": seg.is_hero, "vibe_tags": clip.vibe_tags or [],
        })
        clip_dur[cid] = clip.duration
        clip_meta[cid] = {
            "summary": clip.summary, "setting": clip.setting,
            "vibe_tags": clip.vibe_tags or [], "time_of_day": clip.time_of_day,
            "camera_movement": clip.camera_movement,
        }
        if clip.embedding:
            clip_emb[cid] = clip.embedding
    return segs, clip_dur, clip_meta, clip_emb


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


def _match_clips_to_caption(caption_text: str, clip_meta: dict,
                            clip_quality: dict[str, float] | None = None,
                            max_clips: int = 160) -> list[str]:
    """Rank clip_ids by how well each fits behind the caption (clips react to the caption).

    Candidates are ordered by clip QUALITY (best segment usability) before the cap, so a large
    library offers the ranker its most watchable footage — and if the ranking call fails, the
    fallback order is that same quality order, never arbitrary DB order."""
    q = clip_quality or {}
    # deterministic order (quality desc, id tiebreak) — the clip LISTING is byte-stable between
    # reels, so it rides the prompt cache as a user-prefix block (~11.7k tokens at ~10% on reuse);
    # only the caption tail varies per reel
    items = sorted(clip_meta.items(), key=lambda kv: (-q.get(kv[0], 0.0), kv[0]))
    if len(items) <= 1:
        return [cid for cid, _ in items]
    items = items[:max_clips]
    lines = []
    for i, (_cid, m) in enumerate(items):
        summ = (m.get("summary") or "").strip().replace("\n", " ")[:160]
        vibe = ", ".join((m.get("vibe_tags") or [])[:6])
        lines.append(f"[{i}] {summ}  | vibe: {vibe}")
    clip_block = "CLIPS:\n" + "\n".join(lines)
    tail = f"\nCAPTION:\n{caption_text}\n\nRank the clips above for THIS caption."
    try:
        out = complete_json(_MATCH_SYS, tail, effort="low", max_tokens=1200, tag="clip-match",
                            cache_user_prefix=clip_block)
        start, end = out.find("{"), out.rfind("}")
        order = json.loads(out[start:end + 1]).get("ranked", []) if start != -1 else []
        ranked = [items[i][0] for i in order if isinstance(i, int) and 0 <= i < len(items)]
    except Exception:  # noqa: BLE001 — matching is best-effort; fall back to quality order
        ranked = []
    seen = set(ranked)
    ranked += [cid for cid, _ in items if cid not in seen]
    return ranked


_AUDIO_MATCH_SYS = """You pick the AUDIO that best backs a short-form caption for a 9:16 reel — the track's vibe should AMPLIFY the caption's tone. A blunt / deadpan / contemptuous line wants blunt, hard, aggressive audio; an absurd flex or a hype brag wants upbeat / celebratory; a reflective or sincere line wants slower / atmospheric; a grindset / motivational / no-one-saw-me build wants heavy, locked-in, motivational audio — never playful. Match the ENERGY and the ATTITUDE, not the topic.

Return ONLY JSON, no prose: {"best": <0-based index of the best-fitting audio>}"""


def match_audio(caption: str, audio_descs: list[str]) -> int:
    """Index of the audio whose vibe best amplifies the caption. Falls back to 0 on error / one choice."""
    if len(audio_descs) <= 1:
        return 0
    listing = "\n".join(f"[{i}] {d}" for i, d in enumerate(audio_descs))
    try:
        out = complete_json(_AUDIO_MATCH_SYS, f"CAPTION:\n{caption}\n\nAUDIOS:\n{listing}", effort="low", max_tokens=100, tag="audio-match")
        s, e = out.find("{"), out.rfind("}")
        bi = int(json.loads(out[s:e + 1]).get("best", 0))
        return bi if 0 <= bi < len(audio_descs) else 0
    except Exception:  # noqa: BLE001
        return 0


def generate_caption(niche: str | None, energy: str | None = None) -> tuple[str, list[dict]]:
    """Caption OPTIONS for a reel (audio-agnostic). v3: one variation seed → five separate
    interaction engines (screenshot/send/exotic/mirror/menace) → their outputs ARE the options —
    five different jobs the post could do. The chooser only picks the DEFAULT render — every
    option ships to the operator on the reel card, and their pick (recaption) is the real
    selection. Returns (chosen_text, candidates) with the chosen one flagged."""
    from app.caption.chooser import choose_best
    from app.caption.engine import generate_independent
    cands = generate_independent(k=5, notes=(niche or None), audio_energy=energy)
    if not cands:
        raise RuntimeError("this profile has no voice yet — add caption references to its corpus first")
    texts = [c["text"] for c in cands]
    chosen = choose_best(texts) or texts[0]
    for c in cands:
        c["chosen"] = (c["text"] == chosen)
    return chosen, cands


_DUR_MIN = 5.0   # a reel is just the caption over b-roll — at least this long to read the line + let it land
_DUR_MAX = 9.0   # ...and never longer, even for a long caption


def _target_duration(caption: str) -> float:
    """Reel length SCALES with the caption, clamped to [5s, 9s]: ~3 words/sec silent read + a ~1.8s landing
    beat. A short punchline runs ~5s; a long one caps at 9s — never the full ~15s track."""
    words = len((caption or "").split())
    return max(_DUR_MIN, min(_DUR_MAX, 1.8 + words / 3.0))


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
    caption_candidates: list[dict] | None = None,
    caption_vibe: list[str] | None = None,
    no_caption: bool = False,
    sources: dict[str, str] | None = None,
    clip_ids: list[str] | None = None,
    work_png: str = "tmp/reel_caption.png",
    batch_clip_used: dict[str, int] | None = None,
) -> dict:
    bp = profile.analyze(audio_path)

    segs, clip_dur, clip_meta, clip_emb = _load_segments(clip_ids=clip_ids)
    if not segs:
        raise RuntimeError("no indexed segments available to build a reel")

    # CAPTION FIRST — the caption is the post (a standalone joke). Skipped for blank-caption reels.
    caption_candidates = list(caption_candidates or [])   # pre-generated (audio-first match) or filled below
    if no_caption:
        caption_text = ""
    elif caption_text is None:
        bpm = audio_bpm or bp.bpm
        energy = audio_energy or ("low" if bpm and bpm < 100 else "high" if bpm and bpm > 132 else "mid")
        # BEST-OF-3 caption (audio-agnostic; the chooser picks the one to post). Notes stay MINIMAL.
        caption_text, caption_candidates = generate_caption((niche or "").strip() or None, energy)

    # DURATION SCALES WITH THE CAPTION. A reel is just the caption over b-roll, so it runs only as long as
    # the line needs to be read + land — ~5s short, up to 9s long, never the full track. Cap the beat plan
    # (and, in compose, the audio) to that; a blank reel just stays under the 9s ceiling.
    dur_cap = min(bp.duration, _DUR_MAX) if no_caption else _target_duration(caption_text)
    slots = build_slot_plan(bp.beat_map, bp.duration, max_reel=dur_cap)
    # SINGLE-CLIP STYLE: a profile whose footage is meant as 1-2 clip videos (not a mashup) caps
    # the shot count — the beat plan still times the cut(s), there are just fewer of them.
    from app import profiles
    from app.generate.sequencer import cap_shots
    max_shots = profiles.profile_settings().get("max_shots")
    if max_shots:
        slots = cap_shots(slots, int(max_shots))
    reel_dur = slots[-1].end

    # CAPTION-FIT LEADS: rank the clips by how well each fits THIS caption, then select_segments takes
    # the best-fitting clip per slot, rotating among near-equal fits (by usage) so a small library still
    # varies. Blank reels have no caption to fit, so selection falls back to pure least-used variety.
    clip_quality: dict[str, float] = {}
    for s0 in segs:
        u = s0.get("usability_score") or 0.0
        if u > clip_quality.get(s0["clip_id"], 0.0):
            clip_quality[s0["clip_id"]] = u
    ranked = [] if no_caption else _match_clips_to_caption(caption_text, clip_meta, clip_quality)
    fit_rank = {cid: i for i, cid in enumerate(ranked)}   # clip_id -> fit position (0 = best for this caption)
    clip_text = {cid: (m.get("summary") or "") for cid, m in clip_meta.items()}
    # BATCH-SHARED clip ledger: parallel renders load the SAME clip_usage.json snapshot, so
    # without this, reels in one batch can't see each other's picks and a small library collapses
    # onto the same hero clips every reel. Batch-mates' picks weigh ~3x the cross-reel term
    # (0.45/use in cost units vs 1.5x stored usage) — strong spread pressure, never exclusion.
    usage = _load_clip_usage()
    if batch_clip_used:
        with _USAGE_IO_LOCK:
            snapshot = dict(batch_clip_used)
        usage = dict(usage)
        for cid, cnt in snapshot.items():
            usage[cid] = usage.get(cid, 0) + 3 * cnt
    chosen = select_segments(slots, segs, caption_vibe_tags=caption_vibe,
                             fit_rank=fit_rank, usage=usage, clip_emb=clip_emb,
                             clip_dur=clip_dur, clip_text=clip_text)
    _log_clip_usage([c["clip_id"] for c in chosen])
    if batch_clip_used is not None:
        with _USAGE_IO_LOCK:
            for c in chosen:
                batch_clip_used[c["clip_id"]] = batch_clip_used.get(c["clip_id"], 0) + 1

    if sources is None:
        sources = _resolve_sources(chosen, clip_dur)

    shots = [
        {"src_path": sources[c["clip_id"]], "src_start": c["src_start"], "duration": c["slot_dur"]}
        for c in chosen
    ]

    cap_png = None
    if not no_caption:
        render_caption_png(caption_text, work_png)
        cap_png = work_png
    compose_reel(shots, cap_png, audio_path, out_path, reel_dur)

    # distinct clips actually used + the chosen caption's provenance — for the production-grading record
    clips_used, seen = [], set()
    for c in chosen:
        cid = c["clip_id"]
        if cid not in seen:
            seen.add(cid)
            clips_used.append({"clip_id": cid, "summary": (clip_meta.get(cid) or {}).get("summary")})
    chosen_cand = next((c for c in caption_candidates if c.get("chosen")), None)

    return {"output": out_path, "caption": caption_text, "matched_clips": ranked[:3],
            "duration": round(reel_dur, 2), "shots": len(shots), "sequence": chosen,
            "candidates": caption_candidates,
            "caption_id": chosen_cand.get("caption_id") if chosen_cand else None,
            "caption_anchor_refs": ([chosen_cand["anchor_ref"]] if chosen_cand and chosen_cand.get("anchor_ref") else []),
            "clips": clips_used}
