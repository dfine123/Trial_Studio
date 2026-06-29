"""Grade capture — keep/kill + pairwise preferences on generated candidates.

This is the fuel for the reward model (Layer 2). Pairwise ("A beats B") is the highest-value
signal; keep/kill is the cheap one. Stored as JSONL; trains a scorer once there's enough.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from app.config import settings

GRADES_PATH = os.path.join("var", "grades.jsonl")  # legacy location (pre-profiles); migrated per profile
_LOCK = threading.Lock()  # serialize read-modify-write so rapid grading can't lose/corrupt records


def _grades_path() -> str:
    from app import profiles   # lazy: avoid an import cycle at module load — grading is per ACTIVE PROFILE
    return profiles.grades_path()


def _load_raw() -> list[dict]:
    path = _grades_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _rewrite(records: list[dict]) -> None:
    """Atomic rewrite: write a temp file then os.replace, so the grades file is never left partial."""
    path = _grades_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def record_verdict(caption: str, verdict: str, context: dict | None = None, note: str | None = None) -> None:
    """Upsert one verdict per caption (last wins) so double-clicks/re-clicks never duplicate.

    verdict: 'keep' | 'kill'. note: optional free-text reason (esp. for specific misses).
    """
    with _LOCK:
        recs = [r for r in _load_raw() if not (r.get("type") == "verdict" and r.get("caption") == caption)]
        recs.append({"type": "verdict", "caption": caption, "verdict": verdict, "note": note,
                     "provider": settings.caption_provider, "context": context or {}, "ts": time.time()})
        _rewrite(recs)


def record_pairwise(winner: str, loser: str, context: dict | None = None) -> None:
    """Dedup identical (winner, loser) pairs. (Legacy; ⭐ best now uses record_best.)"""
    with _LOCK:
        recs = _load_raw()
        if any(r.get("type") == "pairwise" and r.get("winner") == winner and r.get("loser") == loser for r in recs):
            return
        recs.append({"type": "pairwise", "winner": winner, "loser": loser, "context": context or {}, "ts": time.time()})
        _rewrite(recs)


def record_best(winner: str, batch: list[str], context: dict | None = None) -> None:
    """One compact 'best of batch' record: `winner` beat the rest of `batch`. Expands to pairwise
    (winner > each other in batch) at training time. Dedups identical winner+batch."""
    with _LOCK:
        recs = _load_raw()
        key_batch = sorted(batch or [])
        for r in recs:
            if r.get("type") == "best" and r.get("winner") == winner and sorted(r.get("batch") or []) == key_batch:
                return
        recs.append({"type": "best", "winner": winner, "batch": list(batch or []),
                     "provider": settings.caption_provider, "context": context or {}, "ts": time.time()})
        _rewrite(recs)


def load_grades() -> list[dict]:
    return _load_raw()


def kept_captions() -> list[str]:
    return [r["caption"] for r in _load_raw() if r.get("type") == "verdict" and r.get("verdict") == "keep"]


def killed_captions() -> list[str]:
    return [r["caption"] for r in _load_raw() if r.get("type") == "verdict" and r.get("verdict") == "kill"]


def best_captions() -> list[str]:
    return [r["winner"] for r in _load_raw() if r.get("type") == "best" and r.get("winner")]


def off_voice_captions() -> list[str]:
    """Captions graded 'not this creator's voice' — a SEPARATE signal from keep/kill (the line may be
    fine, it just isn't them). Used to refine the persona/corpus, not to score the format."""
    return [r["caption"] for r in _load_raw() if r.get("type") == "verdict" and r.get("verdict") == "off_voice"]


def dedupe() -> list[dict]:
    """One-time cleanup of an existing file: one verdict per caption (last wins) + unique pairs."""
    out: list[dict] = []
    vidx: dict[str, int] = {}
    pairs: set = set()
    for r in _load_raw():
        t = r.get("type")
        if t == "verdict":
            cap = r.get("caption")
            if cap in vidx:
                out[vidx[cap]] = r
            else:
                vidx[cap] = len(out)
                out.append(r)
        elif t == "pairwise":
            key = (r.get("winner"), r.get("loser"))
            if key in pairs:
                continue
            pairs.add(key)
            out.append(r)
        else:
            out.append(r)
    _rewrite(out)
    return out
