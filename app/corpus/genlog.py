"""Log of generated captions, so the engine can avoid repeating itself across batches."""
from __future__ import annotations

import json
import os
import time

GEN_PATH = os.path.join("corpus", "generated.jsonl")


def log_generated(captions: list[str]) -> None:
    rows = [c for c in captions if c]
    if not rows:
        return
    os.makedirs(os.path.dirname(GEN_PATH), exist_ok=True)
    with open(GEN_PATH, "a", encoding="utf-8") as f:
        for c in rows:
            f.write(json.dumps({"text": c, "ts": time.time()}, ensure_ascii=False) + "\n")


def recent_generated(n: int = 45) -> list[str]:
    if not os.path.exists(GEN_PATH):
        return []
    with open(GEN_PATH, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [r.get("text", "") for r in rows[-n:]]
