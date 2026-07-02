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


def mark_promoted(reel_id: str, pid=None) -> None:
    """Flag a reel's caption as promoted into the reference corpus (so it never double-promotes)."""
    with _LOCK:
        recs = _load(pid)
        for r in recs:
            if r.get("reel_id") == reel_id:
                r["promoted"] = True
        _rewrite(recs, pid)
