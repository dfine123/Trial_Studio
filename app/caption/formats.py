"""THE FORMAT BOOK — the voice's validated caption VEHICLES, as first-class data.

The operator's law (2026-07-10, from his own grading language): a caption IS its format — the
recognizable vehicle (a would-you-rather, a "dudes be like" flip, a can't-say-X-so-say-Y scene,
an absurd-math ladder, a statement flex...) carrying fresh slot content. His winners are
overwhelmingly format-carried with NEW premises; his dead pile is stale premises in familiar
formats (the template wheel) OR fresh subjects wearing NO format (corny narrations — "as a
caption, what is that even supposed to mean").

So the format is the REUSABLE, LICENSED part (rotated like references used to be: least-used
first, grade-weighted, chronically-dead de-weighted but NEVER dropped — canon 2), and the premise
is the part that must be fresh (taken territory + morph guards). Variety is structural: k
different formats per option set by construction — no caps, no quotas, no shape roster in prose.

Book: var/formats.json (shared across the voice family, like north stars; operator-editable via
/api/formats). Usage: per-voice var/profiles/<voice>/format_usage.json.
"""
from __future__ import annotations

import json
import os
import random
import threading

from app import profiles

_BOOK_PATH = os.path.join("var", "formats.json")
_LOCK = threading.Lock()

# verdict -> virtual-usage penalty (de-weight, never drop — canon 2)
_VERDICT_PENALTY = {"proven-winner": 0, "solid": 0, "mixed": 1, "unproven": 1, "weak": 2, "dead": 3}

# the wildcard pseudo-format: one slot per set stays free for a frameless statement / a shape
# the book doesn't know yet — exploration stays open, but it must read as a POST, not narration
WILDCARD = {
    "id": "freeform",
    "name": "free-form statement",
    "skeleton": "a statement in your exact voice with no frame — an unhinged claim, a coded take, "
                "or a flex held completely straight",
    "what_varies": "everything — but it must read as a thing a guy would POST, never a sentence "
                   "that just narrates or describes something",
    "mechanism": "carried by charge and specificity alone: if it doesn't hit as a post on a cold "
                 "read, it's a narration and it's dead",
    "verdict": "solid",
}


def load_book() -> list[dict]:
    try:
        with open(_BOOK_PATH, encoding="utf-8") as f:
            rows = json.load(f).get("formats", [])
        return [r for r in rows if r.get("id") and r.get("skeleton") and r.get("enabled", True)]
    except Exception:  # noqa: BLE001
        return []


def save_book(formats: list[dict]) -> int:
    os.makedirs(os.path.dirname(_BOOK_PATH) or ".", exist_ok=True)
    tmp = _BOOK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"formats": formats}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _BOOK_PATH)
    return len(formats)


def _usage_path() -> str:
    return os.path.join(os.path.dirname(profiles.ref_usage_path()), "format_usage.json")


def _load_usage() -> dict:
    try:
        with open(_usage_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def log_use(format_ids: list[str]) -> None:
    with _LOCK:
        usage = _load_usage()
        for fid in format_ids:
            usage[fid] = usage.get(fid, 0) + 1
        path = _usage_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(usage, f)
        os.replace(tmp, path)


def pick_formats(k: int) -> list[dict]:
    """k DISTINCT formats for one option set: least-used-first rotation, verdict-de-weighted
    (dead formats recur ~3 cycles later, never vanish), ties shuffled. One WILDCARD slot per
    set of >=5 keeps exploration open. Falls back to wildcards if the book is empty."""
    book = load_book()
    if not book:
        return [dict(WILDCARD) for _ in range(max(1, k))]
    usage = _load_usage()
    rows = sorted(
        book,
        key=lambda r: (usage.get(r.get("id"), 0)
                       + _VERDICT_PENALTY.get((r.get("verdict") or "unproven"), 1)
                       + random.random()),
    )
    n_real = k - 1 if k >= 5 else k
    picked = rows[:n_real]
    if len(picked) < n_real:   # small book: cycle again rather than starve the set
        pool = [r for r in rows if r not in picked] or rows
        while len(picked) < n_real:
            picked.append(random.choice(pool))
    if k >= 5:
        pos = random.randrange(len(picked) + 1)
        picked.insert(pos, dict(WILDCARD))
    return picked[:k]


def assignments_block(picked: list[dict]) -> str:
    """The Stage-A format assignments: skeleton + freshness demand + mechanism. Skeletons carry
    ABSTRACTED slots (never a verbatim winner — the wall holds the real instances; quoting
    winners into instructions is the documented super-attractor failure)."""
    lines = []
    for i, f in enumerate(picked):
        lines.append(
            f"[{i}] {f.get('name') or f.get('id')} — shape: {f.get('skeleton')}"
            + (f" — fresh here: {f.get('what_varies')}" if f.get("what_varies") else "")
            + (f" — what makes it land: {f.get('mechanism')}" if f.get("mechanism") else "")
        )
    return "\n".join(lines)
