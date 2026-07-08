"""NORTH STARS — operator-curated gold-standard captions from the wild (not the posted catalog).

These are the BAR: lines the operator holds up as "this is what great looks like" — used in
generation (the level + the sound), never as premises to reuse. Shared across profiles (the
voice family shares one standard); stored on the volume so intake survives deploys.
"""
from __future__ import annotations

import json
import os
import time
import uuid

_PATH = os.path.join("var", "north_stars.jsonl")


def load() -> list[dict]:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:  # noqa: BLE001
        return []


def add(caption: str, point: str | None = None, stance: str | None = None) -> dict:
    cap = (caption or "").strip()
    if not cap:
        raise ValueError("empty caption")
    rows = load()
    norm = " ".join(cap.lower().split())
    for r in rows:
        if " ".join((r.get("caption") or "").lower().split()) == norm:
            return r   # idempotent
    row = {"ns_id": uuid.uuid4().hex[:8], "caption": cap,
           "point": (point or "").strip() or None,
           "stance": (stance or "").strip() or None,
           "added": time.strftime("%Y-%m-%d")}
    os.makedirs(os.path.dirname(_PATH) or ".", exist_ok=True)
    with open(_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def remove(ns_id: str) -> bool:
    rows = load()
    kept = [r for r in rows if r.get("ns_id") != ns_id]
    if len(kept) == len(rows):
        return False
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, _PATH)
    return True


def block() -> str:
    """The BAR as prompt text: caption + its decoded point (teaches point-first by example)."""
    rows = load()
    if not rows:
        return ""
    lines = []
    for r in rows:
        cap = (r.get("caption") or "").replace("\n", " / ").strip()
        pt = (r.get("point") or "").strip()
        lines.append("- " + cap + (f"\n   (the point: {pt})" if pt else ""))
    return "\n".join(lines)
