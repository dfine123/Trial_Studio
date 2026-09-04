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
import time
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


def cap_shots(slots: list[Slot], max_shots: int | None) -> list[Slot]:
    """Merge a beat-cut slot plan down to at most `max_shots` shots — for profiles whose clips
    are meant as 1-2 clip videos rather than mashups. Cut points stay ON existing slot
    boundaries (still beat-aligned): pick the boundaries nearest to even time splits."""
    if not max_shots or max_shots < 1 or len(slots) <= max_shots:
        return slots
    start, end = slots[0].start, slots[-1].end
    total = end - start
    bounds = [s.end for s in slots[:-1]]
    cuts: list[float] = []
    for j in range(1, max_shots):
        target = start + total * j / max_shots
        candidates = [b for b in bounds if b not in cuts and (not cuts or b > cuts[-1])]
        if not candidates:
            break
        cuts.append(min(candidates, key=lambda b: abs(b - target)))
    pts = [start] + cuts + [end]
    merged = [Slot(idx=i, start=pts[i], end=pts[i + 1])
              for i in range(len(pts) - 1) if pts[i + 1] > pts[i] + 0.05]
    return merged or slots


def build_slot_plan(
    beat_map: list[float],
    audio_duration: float,
    target_shot: float | None = None,
    min_shot: float | None = None,
    max_reel: float | None = None,
    max_shot: float | None = None,
) -> list[Slot]:
    """Group beats into ~target_shot-second shots; every cut falls on a beat.

    Slots are then CAPPED at max_shot: a sparse or empty beat map must never produce one
    giant slot — no clip is 6s+ long, so an uncappable slot ends as a frozen half-reel.
    A capped cut lands mid-beat, which beats dead footage every time."""
    target_shot = target_shot or settings.reel_target_shot
    min_shot = min_shot or settings.reel_min_shot
    max_shot = max_shot or settings.reel_max_shot
    end_cap = min(audio_duration, max_reel) if max_reel else audio_duration

    beats = sorted(b for b in (beat_map or []) if 0.0 < b < end_cap)
    cuts: list[tuple[float, float]] = []
    if beats:
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
    if not cuts:
        cuts.append((0.0, end_cap))

    # cap: split any over-long slot into equal parts <= max_shot
    capped: list[tuple[float, float]] = []
    for s, e in cuts:
        d = e - s
        if d <= max_shot:
            capped.append((s, e))
            continue
        parts = math.ceil(d / max_shot)
        step = d / parts
        capped += [(s + i * step, s + (i + 1) * step) for i in range(parts)]

    return [Slot(i, round(s, 3), round(e, 3)) for i, (s, e) in enumerate(capped)]


def split_slots_at(slots: list[Slot], boundaries: list[float],
                   min_piece: float = 0.4) -> list[Slot]:
    """Force a cut at each boundary time (a caption change must land ON a cut — the reference
    format always changes text on a scene change). A boundary inside a slot splits it when both
    pieces are >= min_piece; otherwise the NEAREST existing cut slides onto the boundary so the
    caption switch still coincides with a cut without creating a micro-shot."""
    if not slots:
        return slots
    pts = [s.start for s in slots] + [slots[-1].end]
    lo, hi = pts[0], pts[-1]
    for b in sorted(boundaries):
        if b <= lo + min_piece or b >= hi - min_piece:
            continue
        if any(abs(b - p) < 0.05 for p in pts):
            continue
        # inner cut points only — the reel's ends never move
        inner = pts[1:-1]
        i = next((j for j in range(len(pts) - 1) if pts[j] < b < pts[j + 1]), None)
        if i is None:
            continue
        if (b - pts[i]) >= min_piece and (pts[i + 1] - b) >= min_piece:
            pts.insert(i + 1, b)
        elif inner:
            nearest = min(range(1, len(pts) - 1), key=lambda j: abs(pts[j] - b))
            moved = sorted(pts[:nearest] + [b] + pts[nearest + 1:])
            # never collapse a neighboring shot below min_piece by sliding
            if all(moved[j + 1] - moved[j] >= min_piece for j in range(len(moved) - 1)):
                pts = moved
    return [Slot(i, round(pts[i], 3), round(pts[i + 1], 3)) for i in range(len(pts) - 1)]


def _cos(a: list, b: list) -> float:
    """Cosine similarity (pure python — small candidate sets, no numpy needed here)."""
    try:
        num = sum(x * y for x, y in zip(a, b))
        da = math.sqrt(sum(x * x for x in a))
        db = math.sqrt(sum(y * y for y in b))
        return num / (da * db) if da and db else 0.0
    except TypeError:
        return 0.0


_STOP = {"the", "a", "an", "of", "in", "on", "at", "with", "and", "or", "is", "are", "to",
         "video", "clip", "opens", "captures", "shows", "features", "view", "person", "man",
         "wearing", "seated", "while", "then", "as", "into", "from", "by", "his", "her"}


def _subject_words(text: str) -> set[str]:
    """Distinctive content words of a clip summary — the SUBJECT fingerprint. Two different
    clips can star the same subject (the same iced-out watch shot twice); embeddings sit near
    zero for those (different scenes), so subject de-dup works on the words instead."""
    return {w for w in "".join(ch if ch.isalnum() or ch == "-" else " "
                               for ch in (text or "").lower()).split()
            if len(w) > 3 and w not in _STOP}


def _same_subject(a: set[str], b: set[str], thr: float = 0.5) -> bool:
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= thr


_FAMILY_SIM = 0.45   # cosine at/above which two clips read as the SAME scene family (recreations)
_FRESH_DAYS = 21.0        # a clip is "new" for three weeks, linearly decaying
_FRESH_BONUS = 3.0        # head start at upload, in fit-rank positions (0.8/use rotation scale)
_NEAR_DUP = 0.82     # at/above this two clips read as the same shot — redundant, not coherent
_SAME_WORLD = 0.35   # at/above this they read as the same world/aesthetic — the sweet spot
_LUM_JUMP = 0.35     # brightness delta between consecutive shots that reads as a broken cut


def select_segments(
    slots: list[Slot],
    segments: list[dict],
    caption_vibe_tags: list[str] | None = None,
    fit_rank: dict[str, int] | None = None,
    usage: dict[str, int] | None = None,
    min_seg: float = 0.8,
    temperature: float = 2.0,
    clip_emb: dict[str, list] | None = None,
    clip_dur: dict[str, float] | None = None,
    clip_text: dict[str, str] | None = None,
    clip_meta: dict[str, dict] | None = None,
    coherent: bool = False,
    prev_seg_in: dict | None = None,
) -> list[dict]:
    """Assign a segment to each slot. CAPTION-FIT LEADS, VARIANCE IS SAMPLED. Each clip gets a COST =
    its caption-fit position (`fit_rank`, 0 = best) + a STRONG within-reel reuse penalty (distinct shots)
    + a cross-reel `usage` penalty (rotation) − a small vibe/quality bonus. Instead of always taking the
    lowest-cost clip, we SAMPLE one with probability ∝ exp(−cost/temperature): the best fit is the most
    likely, but the next few fits each get a real share — so successive reels genuinely vary their footage
    instead of the fit ranker (a single greedy call that structurally CAN'T create variety) landing on the
    same hero clips every time. Higher `temperature` = more variety; clearly-bad-fit clips stay rare.
    Shots prefer clips UNUSED in this reel (no repeats, no first/last bookend) with graceful fallbacks
    for tiny libraries; blank reels (empty fit_rank) sample on pure freshness. Returns the reel sequence.

    `coherent` (reference recreations): the reel should read as ONE scene — same subject (the
    same car), same setting. The variety machinery inverts: visual/subject de-dup is skipped and
    candidates that LOOK like what's already playing get a cost bonus, so picks stay in-family.
    Distinct-clip preference and the quality floor still apply."""
    want = {t.lower() for t in (caption_vibe_tags or [])}
    fit_rank = fit_rank or {}
    usage = usage or {}
    worst_fit = (max(fit_rank.values()) + 1) if fit_rank else 0  # unranked clips cost more than any ranked one

    def vibe_score(seg: dict) -> int:
        return len({t.lower() for t in (seg.get("vibe_tags") or [])} & want)

    meta = clip_meta or {}

    # FRESHNESS (2026-09-04, operator: newly uploaded footage must actually get used). Rotation
    # only stops a clip being over-picked; it gives a brand-new clip no pull of its own, so good
    # new footage could sit at zero uses behind clips with hundreds of reels of momentum. This is
    # a bounded head start that DECAYS to nothing over _FRESH_DAYS — worth a few fit positions on
    # a clip that already fits, never enough to seat one that doesn't.
    _now = time.time()

    def _freshness(cid: str) -> float:
        ts = (meta.get(cid) or {}).get("created_ts")
        if not ts:
            return 0.0
        age_days = max(0.0, (_now - float(ts)) / 86400.0)
        if age_days >= _FRESH_DAYS:
            return 0.0
        return _FRESH_BONUS * (1.0 - age_days / _FRESH_DAYS)

    def _tod(cid: str) -> str:
        v = (meta.get(cid) or {}).get("time_of_day") or ""
        return v if v not in ("unknown", "") else ""

    def cost(s: dict, clip_used: dict[str, int], used_vecs: list[tuple[str, list]],
             prev: dict | None = None) -> float:
        cid = s["clip_id"]
        base = (fit_rank.get(cid, worst_fit)                       # caption fit LEADS (0 = best)
                + (8.0 if coherent else 4.0) * clip_used.get(cid, 0)   # distinct shots within a reel
                + 0.8 * usage.get(cid, 0)                          # rotation: a TIEBREAK only
                - _freshness(cid)                                  # newly uploaded, decaying
                - 0.7 * vibe_score(s)                              # audio-vibe bonus
                - 0.5 * (s.get("usability_score") or 0.0))         # clip-quality bonus
        if used_vecs and clip_emb:
            # Similarity to the OTHER clips already in the reel — never to ITSELF (a used clip
            # is cosine-1.0 with its own vector, which once made repeating the playing shot
            # cheaper than any fresh one).
            sim = max((_cos(clip_emb.get(cid) or [], v) for ucid, v in used_vecs if ucid != cid),
                      default=0.0)
            if coherent:
                # Recreations hold ONE scene — but "one scene" is not "one shot". A monotonic
                # pull made the most identical clip the cheapest, so a recreation could run the
                # same angle three times (operator, 2026-07-22: three POV clips are fine, the
                # exact same thing is not). Same scene is rewarded; the same SHOT is not.
                if sim >= _NEAR_DUP:
                    base += 5.0            # same angle again — not intentional, just repetition
                elif sim >= _FAMILY_SIM:
                    base -= 8.0            # same scene, a different look at it
                else:
                    base += 2.0            # outside the scene
            elif sim >= _NEAR_DUP:
                # A BAND, not a slope (2026-07-22): "belongs together" and "redundant" are
                # different things, and a monotonic bonus can't tell them apart — it actively
                # rewarded near-identical framings (two driver-POV shots in one reel).
                base += 6.0                # the same shot in different clothes — sloppy
            elif sim >= _SAME_WORLD:
                base -= 4.0                # same world, different shot — what a reel wants
            else:
                base += 2.5                # unrelated world — the jarring mash-up
        if prev is not None:
            # CONTINUITY with the shot before it: a cut reads as intentional when the world
            # holds across it. These axes are indexed per clip and were never used in selection.
            pcid = prev["clip_id"]
            a, b = _tod(cid), _tod(pcid)
            if a and b and a != b:
                base += 3.0                                       # day cutting straight to night
            la, lb = s.get("luminance"), prev.get("luminance")
            if la is not None and lb is not None:
                base += 8.0 * max(0.0, abs(la - lb) - 0.20)       # dark clip into a bright one
            if not coherent:
                # (skipped for recreations: one setting is exactly what they're for — there the
                # near-duplicate check is what separates "another angle" from "the same shot")
                m, pm = meta.get(cid) or {}, meta.get(pcid) or {}
                if (m.get("setting") and m.get("setting") == pm.get("setting")
                        and m.get("camera_movement") == pm.get("camera_movement")):
                    base += 3.0                                   # same place, same camera
        return base

    chosen: list[dict] = []
    chosen_segs: list[dict] = []      # the source segments picked (luminance/meta for continuity)
    clip_used: dict[str, int] = {}
    used_vecs: list[tuple[str, list]] = []   # (clip_id, embedding) already in this reel
    word_sets = {cid: _subject_words(t) for cid, t in (clip_text or {}).items()}
    used_words: list[set[str]] = []   # subject fingerprints already in this reel

    # QUALITY FLOOR — footage that "shows nothing" never plays behind a caption. Zero-metric
    # segments are phantom footage (sampling found no frames); near-black or hopelessly blurry
    # ones read as dead air. Tiered so a small/dim library degrades gracefully instead of emptying.
    def _watchable(s: dict, floor: float) -> bool:
        u = s.get("usability_score")
        lum = s.get("luminance")
        if u is not None and u < floor:
            return False
        return not (lum is not None and lum < 0.05)
    watchable = [s for s in segments if _watchable(s, 0.22)] or \
                [s for s in segments if _watchable(s, 0.08)] or list(segments)

    for slot in slots:
        length = slot.duration
        # prefer segments long enough to fill the slot; relax if none qualify
        pool = [s for s in watchable if (s.get("duration") or 0.0) >= length] or \
               [s for s in watchable if (s.get("duration") or 0.0) >= min_seg] or list(watchable)
        # DISTINCT footage within a reel — by ID *and* by LOOK. Different clip ids can be near-identical
        # takes of the same scene (embedding cosine >= threshold = "the same clip" to a viewer), so the
        # preference chain is: visually-distinct unused -> id-distinct unused -> not-consecutive -> pool.
        # continuity carries ACROSS a dynamic recreation's span boundary: each span is selected
        # in its own call, so without this the first shot of a new span answers to nothing and a
        # day/night or brightness jump lands exactly on the caption change.
        prev_clip = chosen[-1]["clip_id"] if chosen else (prev_seg_in or {}).get("clip_id")
        prev_seg = chosen_segs[-1] if chosen_segs else prev_seg_in
        fresh = [s for s in pool if s["clip_id"] not in clip_used]
        cands = fresh
        if coherent:
            # ONE scene, in strict tiers (cost alone left these decisions on a knife-edge that
            # sampling flipped — the repeated-shot bug the operator caught):
            #   1. UNUSED clips from the scene already playing  → never repeat while the family
            #      still has footage
            #   2. used clips from that family (not back-to-back) → a repeat beats cutting to a
            #      different scene once the family is exhausted
            #   3. anything fresh / the pool                     → first slot, or no family yet
            def _fam(seg) -> bool:
                if not used_vecs or not clip_emb:
                    return True
                v = clip_emb.get(seg["clip_id"]) or []
                return max((_cos(v, uv) for _, uv in used_vecs), default=0.0) >= _FAMILY_SIM
            fam_fresh = [s for s in fresh if _fam(s)]
            # …and inside the family, a shot that isn't a near-duplicate of one already playing
            fam_distinct = [s for s in fam_fresh
                            if max((_cos(clip_emb.get(s["clip_id"]) or [], v)
                                    for ucid, v in used_vecs if ucid != s["clip_id"]),
                                   default=0.0) < _NEAR_DUP] if (used_vecs and clip_emb) else fam_fresh
            fam_used = [s for s in pool if s["clip_id"] != prev_clip
                        and s["clip_id"] in clip_used and _fam(s)]
            cands = fam_distinct or fam_fresh or fam_used or [s for s in (fresh or pool)
                                                              if s["clip_id"] != prev_clip] or pool
        else:
            # (2026-07-22) the old anti-lookalike + same-subject EXCLUSIONS forced every slot
            # into a different visual family — the "completely unrelated clips" failure. Same
            # subject/look across shots is what a coherent reel IS; only literal repetition is
            # limited (distinct-id preference + the 4.0 reuse penalty + no back-to-back).
            cands = [s for s in (fresh or pool) if s["clip_id"] != prev_clip] or fresh or pool
            # THE CLEAN TIER (2026-07-22): a penalty can always be out-argued by a better
            # caption-fit — which is how two driver-POV shots kept landing in one reel (they're
            # the best fits AND near-identical), and how a bright clip landed in a night reel.
            # So candidates that are BOTH visually fresh and continuous with the shot before are
            # preferred outright; the scalar penalties only arbitrate when no such clip exists
            # (a small or one-note library), instead of forcing one flaw to avoid another.
            def _dup(seg: dict) -> bool:
                if not (used_vecs and clip_emb):
                    return False
                return max((_cos(clip_emb.get(seg["clip_id"]) or [], v)
                            for ucid, v in used_vecs if ucid != seg["clip_id"]),
                           default=0.0) >= _NEAR_DUP

            def _breaks(seg: dict) -> bool:
                if prev_seg is None:
                    return False
                a, b = _tod(seg["clip_id"]), _tod(prev_seg["clip_id"])
                if a and b and a != b:
                    return True
                la, lb = seg.get("luminance"), prev_seg.get("luminance")
                return la is not None and lb is not None and abs(la - lb) > _LUM_JUMP

            clean = [s for s in cands if not _dup(s) and not _breaks(s)]
            cands = clean or cands
        cands = cands or [s for s in pool if s["clip_id"] != prev_clip] or pool
        scored = sorted(((cost(s, clip_used, used_vecs, prev_seg), s) for s in cands),
                        key=lambda cs: cs[0])
        scored = scored[:6]   # sample within the top fits only — a bad fit never plays
        costs = [c for c, _ in scored]
        cands = [s for _, s in scored]
        lo = min(costs)
        weights = [math.exp(-(c - lo) / max(temperature, 1e-6)) for c in costs]
        seg = random.choices(cands, weights=weights, k=1)[0]   # SAMPLE — variance is intrinsic, not forced
        chosen_segs.append(seg)
        clip_used[seg["clip_id"]] = clip_used.get(seg["clip_id"], 0) + 1
        if clip_emb and clip_emb.get(seg["clip_id"]):
            used_vecs.append((seg["clip_id"], clip_emb[seg["clip_id"]]))
        if word_sets.get(seg["clip_id"]):
            used_words.append(word_sets[seg["clip_id"]])

        seg_dur = seg.get("duration") or length
        offset = max(0.0, (seg_dur - length) / 2.0)    # center the sub-window in the segment
        src_start = (seg.get("start_ts") or 0.0) + offset
        # never cut past the clip's REAL footage — shift the window left instead (a window past
        # the last frame renders zero frames and the reel freezes)
        real_end = (clip_dur or {}).get(seg["clip_id"])
        if real_end:
            src_start = max(0.0, min(src_start, real_end - length - 0.05))
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
                "luminance": seg.get("luminance"),   # carried so continuity works across spans
                "vibe_tags": seg.get("vibe_tags"),
            }
        )
    return chosen
