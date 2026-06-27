"""Corpus store + retrieval over references.jsonl.

v1 retrieval is tag/mode-based (the corpus is small and richly labeled, so this beats noisy
embeddings on a tiny set). Swap in vector retrieval here once the corpus is large — the
interface (`retrieve`) stays the same.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

CORPUS_PATH = os.path.join("corpus", "references.jsonl")   # legacy seed location (pre-profiles)


def load_refs(path: str | None = None) -> list[dict]:
    if path is None:
        from app import profiles   # lazy: avoid an import cycle at module load
        path = profiles.corpus_path()
    refs: list[dict] = []
    if not os.path.exists(path):
        return refs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                refs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return refs


def mode_distribution(refs: list[dict] | None = None) -> Counter:
    refs = refs if refs is not None else load_refs()
    return Counter(r.get("persona_trait", "?") for r in refs)


def retrieve(
    refs: list[dict] | None = None,
    target_modes: list[str] | None = None,
    n: int = 10,
    exclude_captions: list[str] | None = None,
) -> list[dict]:
    """Return up to n diverse, relevant references.

    Round-robins across persona_trait so the injected set spans the voice instead of 10 of
    one mode, while biasing toward target_modes (the modes that fit the chosen audio).
    """
    refs = refs if refs is not None else load_refs()
    exclude = set(exclude_captions or [])
    pool = [r for r in refs if r.get("caption") not in exclude]

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in pool:
        groups[r.get("persona_trait", "?")].append(r)

    target = set(target_modes or [])
    # target modes first, then richest groups — gives relevance + diversity
    trait_order = sorted(groups.keys(), key=lambda t: (t not in target, -len(groups[t])))

    picked: list[dict] = []
    while len(picked) < n:
        progressed = False
        for t in trait_order:
            if groups[t]:
                picked.append(groups[t].pop(0))
                progressed = True
                if len(picked) >= n:
                    break
        if not progressed:
            break
    return picked
