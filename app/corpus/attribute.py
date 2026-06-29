"""In-process, per-profile, EXACT grade attribution — the closed loop's write side.

Every keep/kill/best grade is credited back to the anchor reference(s) it came from (carried on the
candidate as anchor_refs and forwarded by the grade UI in context). This updates the ACTIVE profile's
ref_scores.json, which engine._pick_anchors reads to weight the rotation (amplify winners, drop
chronically-killed formats). Replaces the manual tmp/attribute_grades.sh, which wrote a GLOBAL
ref_scores.json the per-profile engine never read — so the loop had silently been open since the
per-profile migration.

off_voice is deliberately NOT credited here: it means "good line, wrong voice" — a persona signal,
not a format signal — so it must not penalize the reference. Embedding-nearest backfill of the
anchor-less legacy grades lands with the embedding index (a later phase).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid

from app import profiles

_LOCK = threading.Lock()  # serialize read-modify-write so rapid grading can't lose a bump


def _anchor_refs(context: dict | None) -> list[str]:
    """The anchor ref ids a grade should credit. Prefers the anchor_refs list (new), falls back to
    the singular anchor (older candidates) so legacy-shaped contexts still attribute."""
    ctx = context or {}
    refs = ctx.get("anchor_refs")
    if isinstance(refs, list):
        out = [r for r in refs if r]
        if out:
            return out
    a = ctx.get("anchor")
    return [a] if a else []


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _bump(refs: list[str], field: str, pid: uuid.UUID | None) -> None:
    if not refs:
        return
    path = profiles.ref_scores_path(pid)
    with _LOCK:
        scores = _load(path)
        for rid in refs:
            s = scores.setdefault(rid, {"keep": 0, "kill": 0, "best": 0})
            s[field] = int(s.get(field, 0)) + 1
        _atomic_write(path, scores)


def credit_verdict(context: dict | None, verdict: str, pid: uuid.UUID | None = None) -> None:
    """keep/kill bump the anchor ref(s). off_voice is a persona signal — not credited here."""
    if verdict in ("keep", "kill"):
        _bump(_anchor_refs(context), verdict, pid)


def credit_best(context: dict | None, pid: uuid.UUID | None = None) -> None:
    """The chosen 'best of batch' caption's anchor ref(s) get a best credit (weighted up in rotation)."""
    _bump(_anchor_refs(context), "best", pid)
