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

import math
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
    temperature: float = 2.0,
) -> list[dict]:
    """Assign a segment to each slot. CAPTION-FIT LEADS, VARIANCE IS SAMPLED. Each clip gets a COST =
    its caption-fit position (`fit_rank`, 0 = best) + a STRONG within-reel reuse penalty (distinct shots)
    + a cross-reel `usage` penalty (rotation) − a small vibe/quality bonus. Instead of always taking the
    lowest-cost clip, we SAMPLE one with probability ∝ exp(−cost/temperature): the best fit is the most
    likely, but the next few fits each get a real share — so successive reels genuinely vary their footage
    instead of the fit ranker (a single greedy call that structurally CAN'T create variety) landing on the
    same hero clips every time. Higher `temperature` = more variety; clearly-bad-fit clips stay rare.
    Shots prefer clips UNUSED in this reel (no repeats, no first/last bookend) with graceful fallbacks
    for tiny libraries; blank reels (empty fit_rank) sample on pure freshness. Returns the reel sequence."""
    want = {t.lower() for t in (caption_vibe_tags or [])}
    fit_rank = fit_rank or {}
    usage = usage or {}
    worst_fit = (max(fit_rank.values()) + 1) if fit_rank else 0  # unranked clips cost more than any ranked one

    def vibe_score(seg: dict) -> int:
        return len({t.lower() for t in (seg.get("vibe_tags") or [])} & want)

    def cost(s: dict, clip_used: dict[str, int]) -> float:
        cid = s["clip_id"]
        return (fit_rank.get(cid, worst_fit)                       # caption fit LEADS (0 = best)
                + 4.0 * clip_used.get(cid, 0)                      # strong: distinct shots within a reel
                + 1.5 * usage.get(cid, 0)                          # rotate across reels
                - 0.7 * vibe_score(s)                              # audio-vibe bonus
                - 0.5 * (s.get("usability_score") or 0.0))         # clip-quality bonus

    chosen: list[dict] = []
    clip_used: dict[str, int] = {}

    for slot in slots:
        length = slot.duration
        # prefer segments long enough to fill the slot; relax if none qualify
        pool = [s for s in segments if (s.get("duration") or 0.0) >= length] or \
               [s for s in segments if (s.get("duration") or 0.0) >= min_seg] or list(segments)
        # DISTINCT clips within a reel: a repeat — especially the same clip bookending the first and
        # last shot — reads as a glitch/loop. Prefer clips unused in this reel; fall back to merely
        # not-consecutive, then to the raw pool (only a library smaller than the slot count gets there).
        prev_clip = chosen[-1]["clip_id"] if chosen else None
        cands = ([s for s in pool if s["clip_id"] not in clip_used]
                 or [s for s in pool if s["clip_id"] != prev_clip]
                 or pool)
        costs = [cost(s, clip_used) for s in cands]
        lo = min(costs)
        weights = [math.exp(-(c - lo) / max(temperature, 1e-6)) for c in costs]
        seg = random.choices(cands, weights=weights, k=1)[0]   # SAMPLE — variance is intrinsic, not forced
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
