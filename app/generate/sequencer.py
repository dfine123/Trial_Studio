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
    min_seg: float = 0.8,
) -> list[dict]:
    """Assign a segment to each slot. segments: dicts with id, clip_id, start_ts, duration,
    usability_score, energy, is_hero, vibe_tags. Returns the reel sequence."""
    want = {t.lower() for t in (caption_vibe_tags or [])}

    def vibe_score(seg: dict) -> int:
        return len({t.lower() for t in (seg.get("vibe_tags") or [])} & want)

    chosen: list[dict] = []
    used: dict[str, int] = {}

    for slot in slots:
        length = slot.duration
        # prefer segments long enough to fill the slot; relax if none qualify
        pool = [s for s in segments if (s.get("duration") or 0.0) >= length] or \
               [s for s in segments if (s.get("duration") or 0.0) >= min_seg] or list(segments)
        prev = chosen[-1]["segment_id"] if chosen else None
        ranked = sorted(
            [s for s in pool if s["id"] != prev] or pool,
            key=lambda s: (
                vibe_score(s),
                -used.get(s["id"], 0),                 # rotation: prefer less-used
                1 if s.get("is_hero") else 0,
                s.get("usability_score") or 0.0,
            ),
            reverse=True,
        )
        seg = ranked[0]
        used[seg["id"]] = used.get(seg["id"], 0) + 1

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
