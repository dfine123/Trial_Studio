"""Selection layer — picks the single best of N independently-generated candidate captions.

Best-of-N sampling: generate candidates INDEPENDENTLY (separate calls, no shared batch / avoid-list
cross-suppression), then this layer — the creator's gut — chooses the one to actually post. It's a
CHOOSER ("which one would you post?"), NOT a 0-10 scorer (scoring rubrics added noise and were
dropped). Raises per-caption quality: the max of 3 independent shots beats any single shot.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You ARE this creator, staring at a few of your own draft captions and picking the ONE you'd actually post. Pick the one with the sharpest twist, the most hyper-specific and very-online detail, the most "screenshot it and send it to the group chat" energy — the one most unmistakably YOU. Kill anything that reads generic, corporate, soft/poetic, factually off, or like a watered-down version of a better idea. Trust your gut.

Return ONLY JSON, no prose: {"best": <0-based index of the single best caption>}"""


def choose_best(candidates: list[str]) -> str:
    """Return the single best caption to post. Falls back to the first on any error."""
    cands = [c for c in candidates if (c or "").strip()]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    listing = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(cands))
    try:
        out = complete_json(_SYS, f"Pick the ONE you'd actually post:\n\n{listing}", effort="medium", max_tokens=500)
        s, e = out.find("{"), out.rfind("}")
        best = int(json.loads(out[s:e + 1]).get("best", 0))
        if 0 <= best < len(cands):
            return cands[best]
    except Exception:  # noqa: BLE001 — chooser must never break generation
        pass
    return cands[0]
