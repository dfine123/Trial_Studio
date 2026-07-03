"""Permanently retired references — dropped by the operator, gone everywhere, never negative-prompted.

Removing a reference from `corpus/references.jsonl` only stops NEW profiles from seeding it; every
existing profile already copied it onto the volume at first boot, and there is no ref-delete API. This
module purges the retired refs from EVERY profile's live voice files — references.jsonl (the corpus),
ref_scores.json (grade attribution) and ref_usage.json (rotation) — at startup, so a dropped reference
is gone from generation entirely and can't resurface via a stale volume copy.

Idempotent + self-healing: a file is only rewritten when it actually changes, so it runs free on every
boot once clean. To retire another reference later, add its id (and, to be safe, its exact caption) below.

History (generated.jsonl / grades.jsonl / reels.jsonl) is intentionally left untouched — those are
append-only logs of what was posted/graded, not the live corpus; a retired ref sitting in that history
is inert (generation only ever reads references.jsonl).
"""
from __future__ import annotations

import json
import os
import tempfile

from sqlalchemy import select

from app import profiles
from app.db import SessionLocal
from app.models import User

# Matched by exact CAPTION only. Ref ids are PROFILE-LOCAL and renumbered on seeding (a verbatim seed
# gave another profile's innocent 14th ref the id "r014"), so id matching would delete legitimate refs.
# (Originally Spence's r014.)
RETIRED_CAPTIONS: set[str] = {
    "She should be serving LIFE for animal abuse. The way she treated the GOAT.",
}


def _is_retired(ref: dict) -> bool:
    return (ref.get("caption") or "") in RETIRED_CAPTIONS


def _atomic_write(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _purge_corpus(path: str) -> int:
    """Drop retired refs from a references.jsonl. Returns how many were removed."""
    if not os.path.exists(path):
        return 0
    kept: list[str] = []
    removed = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                ref = json.loads(s)
            except json.JSONDecodeError:
                kept.append(s)   # keep unparseable lines verbatim — never destroy data we can't classify
                continue
            if _is_retired(ref):
                removed += 1
            else:
                kept.append(s)
    if removed:
        _atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))
    return removed


def _purge_json_map(path: str, drop_ids: set[str]) -> int:
    """Drop the given ref_id keys from a ref_scores/ref_usage json map. Returns keys removed."""
    if not drop_ids or not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    drop = [k for k in data if k in drop_ids]
    for k in drop:
        del data[k]
    if drop:
        _atomic_write(path, json.dumps(data, ensure_ascii=False))
    return len(drop)


def purge_profile(pid) -> dict:
    """Remove every retired ref from ONE profile's live voice files. The score/usage keys to drop are
    the ids the retired CAPTION holds in THIS profile's corpus (ids are profile-local)."""
    from app.corpus.store import load_refs
    drop_ids = {r.get("ref_id") for r in load_refs(profiles.corpus_path(pid))
                if _is_retired(r) and r.get("ref_id")}
    return {
        "corpus": _purge_corpus(profiles.corpus_path(pid)),
        "scores": _purge_json_map(profiles.ref_scores_path(pid), drop_ids),
        "usage": _purge_json_map(profiles.ref_usage_path(pid), drop_ids),
    }


def purge_all() -> dict:
    """Remove every retired ref from every profile's live voice files. Self-healing; call at startup.
    Never raises — cleanup must not block boot."""
    if not RETIRED_CAPTIONS:
        return {}
    try:
        with SessionLocal() as s:
            pids = [u.id for u in s.scalars(select(User)).all()]
    except Exception:  # noqa: BLE001
        return {}
    total = {"corpus": 0, "scores": 0, "usage": 0}
    for pid in pids:
        try:
            got = purge_profile(pid)
            for k in total:
                total[k] += got[k]
        except Exception:  # noqa: BLE001
            continue
    return total


def retired_present(pid) -> list[str]:
    """Any retired ref still present in a profile's references.jsonl (for post-purge audit). Empty = clean."""
    from app.corpus.store import load_refs
    present: list[str] = []
    for ref in load_refs(profiles.corpus_path(pid)):
        if _is_retired(ref):
            present.append(ref.get("ref_id") or (ref.get("caption") or "")[:40])
    return present
