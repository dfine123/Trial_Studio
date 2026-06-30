"""Structure-aware sequencer.

Two jobs:
  1. build_slot_plan — turn the audio's librosa beat map into a list of shots. Each cut lands
     ON a beat, but a shot spans multiple beats (~reel_target_shot seconds) — cuts are
     beat-synced, not one-per-beat.
  2. select_segments — fill each slot with the best indexed segment, matching the caption's
     vibe, preferring hero/high-usability clips, rotating usage, avoiding back-to-back repeats.

V1 uses a `steady` structure (even, slightly escalating energy). before/after pivots and hard
coherence locking are Phase 3.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from app.config import settings


@dataclass
class Slot:
    idx: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def build_slot_plan(
    beat_map: list[float],
    audio_duration: float,
    target_shot: float | None = None,
    min_shot: float | None = None,
    max_reel: float | None = None,
) -> list[Slot]:
    """Group beats into ~target_shot-second shots; every cut falls on a beat."""
    target_shot = target_shot or settings.reel_target_shot
    min_shot = min_shot or settings.reel_min_shot
    end_cap = min(audio_duration, max_reel) if max_reel else audio_duration

    beats = sorted(b for b in (beat_map or []) if 0.0 < b < end_cap)
    if not beats:
        return [Slot(0, 0.0, end_cap)]

    cuts: list[tuple[float, float]] = []
    last = 0.0
    for b in beats:
        if b - last >= target_shot:
            cuts.append((last, b))
            last = b

    # tail to the end of the audio
    if end_cap - last >= min_shot:
        cuts.append((last, end_cap))
    elif cuts:
        s, _ = cuts[-1]
        cuts[-1] = (s, end_cap)  # absorb a too-short shard into the previous shot
    else:
        cuts.append((0.0, end_cap))

    return [Slot(i, round(s, 3), round(e, 3)) for i, (s, e) in enumerate(cuts)]


def select_segments(
    slots: list[Slot],
    segments: list[dict],
    caption_vibe_tags: list[str] | None = None,
    fit_rank: dict[str, int] | None = None,
    usage: dict[str, int] | None = None,
    min_seg: float = 0.8,
) -> list[dict]:
    """Assign a segment to each slot. CAPTION-FIT LEADS: each clip scores = its caption-fit position
    (`fit_rank`, 0 = best) plus a rotation penalty — a STRONG one for reuse WITHIN this reel (so shots
    stay distinct) and a MILD one for cross-reel `usage` (so a small library still rotates instead of
    repeating one hero clip). Lowest score wins, so fit dominates and freshness only breaks near-ties.
    Vibe + usability + a random tiebreak settle the rest; consecutive shots avoid the same clip. With no
    caption (blank reel) `fit_rank` is empty, so every clip ties on fit and selection is pure variety.
    Returns the reel sequence."""
    want = {t.lower() for t in (caption_vibe_tags or [])}
    fit_rank = fit_rank or {}
    usage = usage or {}
    worst_fit = (max(fit_rank.values()) + 1) if fit_rank else 0  # unranked clips sort after ranked ones

    def vibe_score(seg: dict) -> int:
        return len({t.lower() for t in (seg.get("vibe_tags") or [])} & want)

    chosen: list[dict] = []
    clip_used: dict[str, int] = {}

    for slot in slots:
        length = slot.duration
        # prefer segments long enough to fill the slot; relax if none qualify
        pool = [s for s in segments if (s.get("duration") or 0.0) >= length] or \
               [s for s in segments if (s.get("duration") or 0.0) >= min_seg] or list(segments)
        prev_clip = chosen[-1]["clip_id"] if chosen else None
        ranked = sorted(
            [s for s in pool if s["clip_id"] != prev_clip] or pool,
            key=lambda s: (
                fit_rank.get(s["clip_id"], worst_fit)                            # caption fit LEADS (0 = best)
                + 4.0 * clip_used.get(s["clip_id"], 0)                           # strong: keep shots distinct within a reel
                + 0.5 * usage.get(s["clip_id"], 0),                              # mild: rotate across reels
                -vibe_score(s),                                                  # audio-vibe match (higher better)
                -(s.get("usability_score") or 0.0),                             # clip quality (higher better)
                random.random(),                                                 # break exact ties
            ),
        )
        seg = ranked[0]
        clip_used[seg["clip_id"]] = clip_used.get(seg["clip_id"], 0) + 1

        seg_dur = seg.get("duration") or length
        offset = max(0.0, (seg_dur - length) / 2.0)    # center the sub-window in the segment
        src_start = (seg.get("start_ts") or 0.0) + offset
        chosen.append(
            {
                "slot": slot.idx,
                "slot_start": slot.start,
                "slot_end": slot.end,
                "slot_dur": round(length, 3),
                "segment_id": seg["id"],
                "clip_id": seg["clip_id"],
                "src_start": round(src_start, 3),
                "src_end": round(src_start + length, 3),
                "is_hero": bool(seg.get("is_hero")),
                "usability": seg.get("usability_score"),
                "vibe_tags": seg.get("vibe_tags"),
            }
        )
    return chosen
