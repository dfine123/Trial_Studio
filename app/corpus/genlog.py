"""Log of generated captions, so the engine can avoid repeating itself across batches."""
from __future__ import annotations

import json
import os
import time

GEN_PATH = os.path.join("corpus", "generated.jsonl")   # legacy location (pre-profiles); migrated per profile


def _path() -> str:
    from app import profiles   # lazy: avoid an import cycle at module load
    return profiles.genlog_path()


def log_generated(captions: list[str]) -> None:
    rows = [c for c in captions if c]
    if not rows:
        return
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for c in rows:
            f.write(json.dumps({"text": c, "ts": time.time()}, ensure_ascii=False) + "\n")


def recent_generated(n: int = 45) -> list[str]:
    path = _path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [r.get("text", "") for r in rows[-n:]]
