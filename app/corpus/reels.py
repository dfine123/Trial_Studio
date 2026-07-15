"""Production reel records — the captured context of each generated reel, for END-OUTPUT grading.

Each record holds the chosen caption + the OTHER candidate captions that were available (best-of-N,
each tagged with the anchor it came from) + the clips used + the audio + the reel file. The operator
grades the finished reel with a /10 rating + notes — a richer training signal than grading captions
in isolation, because it sees the whole output and which captions the chooser passed over. Per-profile
JSONL, atomic + locked like the grade store.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from app import profiles

_LOCK = threading.Lock()


def _path(pid=None) -> str:
    return profiles.reels_path(pid)


def _load(pid=None) -> list[dict]:
    p = _path(pid)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _rewrite(records: list[dict], pid=None) -> None:
    p = _path(pid)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def append(record: dict, pid=None) -> None:
    """Append a freshly-generated reel's record (ungraded). Stamps ts + grade=None."""
    record.setdefault("ts", time.time())
    record.setdefault("grade", None)
    with _LOCK:
        recs = _load(pid)
        recs.append(record)
        _rewrite(recs, pid)


def pending(pid=None) -> list[dict]:
    """Ungraded reels, newest first."""
    return [r for r in reversed(_load(pid)) if not r.get("grade")]


def log_default(caption: str, pid=None) -> None:
    """Log a chosen default at CAPTION time (reel records append only after render, which is
    too late for feed memory inside a pipelined batch — two adjacent cards both ran a Dealer
    scene because neither could see the other). Append-only, profile-owned."""
    cap = (caption or "").strip()
    if not cap:
        return
    p = profiles.feed_log_path(pid)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with _LOCK, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"caption": cap, "ts": time.time()}, ensure_ascii=False) + "\n")


def recent_captions(n: int = 10, pid=None) -> list[str]:
    """The profile's most recent posted/chosen captions, oldest→newest — 'the feed so far' for
    both the slate author and the chooser (2026-07-15 realignment). Merges the reel records
    with the caption-time feed log (which leads them by one render), deduped, newest-last."""
    rows = [(r.get("ts") or 0, (r.get("caption") or "").strip()) for r in _load(pid)]
    try:
        p = profiles.feed_log_path(pid)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        rows.append((r.get("ts") or 0, (r.get("caption") or "").strip()))
    except Exception:  # noqa: BLE001 — feed memory must never break anything
        pass
    rows.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, cap in rows:
        if cap and (not out or cap != out[-1]) and cap not in out[-3:]:
            out.append(cap)
    return out[-n:]


def record_grade(reel_id: str, rating, notes, pid=None) -> dict | None:
    """Set the /10 rating + notes on a reel record (by reel_id). Returns the record, or None if absent."""
    with _LOCK:
        recs = _load(pid)
        target = None
        for r in recs:
            if r.get("reel_id") == reel_id:
                r["grade"] = {"rating": rating, "notes": notes, "ts": time.time()}
                target = r
        _rewrite(recs, pid)
        return target


def graded(pid=None) -> list[dict]:
    """Reels that have been graded (carry a rating/notes) — the production feedback, newest first."""
    return [r for r in reversed(_load(pid)) if r.get("grade")]


def get(reel_id: str, pid=None) -> dict | None:
    """Fetch one reel record by id."""
    return next((r for r in _load(pid) if r.get("reel_id") == reel_id), None)


def record_recaption(reel_id: str, new_url: str, new_caption: str, new_clips: list[dict],
                     pid=None) -> dict | None:
    """The operator picked a DIFFERENT caption option and the reel was re-produced with it.
    Updates the record in place (same reel_id — the card is the same entity) and appends the
    swap to caption_swaps: "picked X over the default Y" is real operator-taste selection
    data (future chooser-eval cases mine it), a signal no LLM judge provides."""
    with _LOCK:
        recs = _load(pid)
        target = None
        for r in recs:
            if r.get("reel_id") != reel_id:
                continue
            old = (r.get("caption") or "").strip()
            r.setdefault("caption_swaps", []).append(
                {"from": old, "to": new_caption, "ts": time.time()})
            r["caption"] = new_caption
            r["reel_url"] = new_url
            if new_clips:
                r["clips"] = new_clips
            matched = False
            for c in r.get("candidates") or []:
                hit = (c.get("text") or "").strip() == new_caption.strip()
                c["chosen"] = hit
                matched = matched or hit
            if not matched:   # operator-authored text still becomes the chosen candidate
                r.setdefault("candidates", []).append(
                    {"text": new_caption, "chosen": True, "operator_authored": True})
            target = r
        _rewrite(recs, pid)
        return target


def mark_promoted(reel_id: str, pid=None) -> None:
    """Flag a reel's caption as promoted into the reference corpus (so it never double-promotes)."""
    with _LOCK:
        recs = _load(pid)
        for r in recs:
            if r.get("reel_id") == reel_id:
                r["promoted"] = True
        _rewrite(recs, pid)
