"""Grade capture — keep/kill + pairwise preferences on generated candidates.

This is the fuel for the reward model (Layer 2). Pairwise ("A beats B") is the highest-value
signal; keep/kill is the cheap one. Stored as JSONL; trains a scorer once there's enough.
"""
from __future__ import annotations

import json
import os
import time

GRADES_PATH = os.path.join("corpus", "grades.jsonl")


def _append(rec: dict) -> None:
    rec["ts"] = time.time()
    os.makedirs(os.path.dirname(GRADES_PATH), exist_ok=True)
    with open(GRADES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record_verdict(caption: str, verdict: str, context: dict | None = None, note: str | None = None) -> None:
    """verdict: 'keep' | 'kill'. note: optional free-text reason (esp. for specific misses)."""
    _append({"type": "verdict", "caption": caption, "verdict": verdict, "note": note, "context": context or {}})


def record_pairwise(winner: str, loser: str, context: dict | None = None) -> None:
    _append({"type": "pairwise", "winner": winner, "loser": loser, "context": context or {}})


def load_grades() -> list[dict]:
    if not os.path.exists(GRADES_PATH):
        return []
    with open(GRADES_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
