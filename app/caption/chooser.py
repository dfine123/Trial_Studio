"""Selection layer — picks the single best of N independently-generated candidate captions.

Best-of-N sampling: generate candidates INDEPENDENTLY (separate calls, no shared batch / avoid-list
cross-suppression), then this layer — the creator's gut — chooses the one to actually post. It's a
CHOOSER ("which one would you post?"), NOT a 0-10 scorer (scoring rubrics added noise and were
dropped). Raises per-caption quality: the max of 3 independent shots beats any single shot.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You ARE this creator, looking at a few of your own draft captions and choosing the ONE you'd actually post. Pick the single best caption AS A WHOLE — reading it cold, which one actually HITS: it makes you stop, it rings true, and the turn LANDS clean in one read — the kind you'd screenshot and send. You know your own range — crude wordplay, villain flex, degenerate confession, absurd bits, self-owns, and genuinely sincere grindset wisdom are ALL you; judge each purely on whether it CONNECTS, never on its topic or its register. A clever idea that fumbles the landing loses to a simpler line that lands. Go with your gut on the one that actually hits.

Return ONLY JSON, no prose: {"best": <0-based index of the single best caption>}"""


def choose_best(candidates: list[str]) -> str:
    """Return the single best caption to post. Falls back to the first on any error."""
    cands = [c for c in candidates if (c or "").strip()]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    listing = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(cands))
    system = _SYS
    try:                                    # the creator's learned TASTE — what makes THEIR captions hit
        from app.caption.taste import distilled_taste
        taste = distilled_taste()
        if taste:
            system = _SYS + "\n\n--- WHAT MAKES YOUR CAPTIONS HIT (distilled from everything you've graded) ---\n" + taste
    except Exception:  # noqa: BLE001
        pass
    try:
        out = complete_json(system, f"Pick the ONE you'd actually post:\n\n{listing}", effort="high", max_tokens=500)
        s, e = out.find("{"), out.rfind("}")
        best = int(json.loads(out[s:e + 1]).get("best", 0))
        if 0 <= best < len(cands):
            return cands[best]
    except Exception:  # noqa: BLE001 — chooser must never break generation
        pass
    return cands[0]
