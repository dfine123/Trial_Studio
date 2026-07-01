"""Selection layer — picks the single best of N independently-generated candidate captions.

Best-of-N sampling: generate candidates INDEPENDENTLY (separate calls, no shared batch / avoid-list
cross-suppression), then this layer — the creator's gut — chooses the one to actually post. It's a
CHOOSER ("which one would you post?"), NOT a 0-10 scorer (scoring rubrics added noise and were
dropped). Raises per-caption quality: the max of 3 independent shots beats any single shot.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You ARE this creator, looking at a few of your own draft captions and picking the ONE you'd actually post — the one you'd screenshot and send. Judge on your gut, across your FULL range: one-liners, lists, POVs, would-you-rathers, developed/layered reframes, sincere grindset wisdom, crude bits, villain flexes, degenerate confessions are ALL you, and the best pick can be ANY of them — never penalize a line for its format, its topic, or its register.

The DECIDER is the LANDING. Pick the one whose payoff actually SNAPS — a sharp, surprising, exact turn you'd stop scrolling for. Pass over the ones that land FLAT even when the premise is fine: a soft or sentimental ending, a mild/obvious observation, an over-explained tail that says the joke twice, or a proven format just mechanically filled in. A LIST or POV that snaps beats a one-liner that fizzles, and vice versa — you are judging the SNAP of the landing, never the length or the shape. If two genuinely tie, take the one that feels most like something you'd actually post, not the safe one.

Return ONLY JSON, no prose: {"best": <0-based index of the single best caption>}"""


def choose_best(candidates: list[str]) -> str:
    """Return the single best caption to post. Falls back to the first on any error."""
    cands = [c for c in candidates if (c or "").strip()]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    listing = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(cands))
    # NOTE: the distilled-taste block was REMOVED here — it narrowed selection toward "tight one-twist" and
    # sanded the range (lists/POV/developed/sincere). Selection stays reference-anchored + full-range (_SYS).
    try:
        out = complete_json(_SYS, f"Pick the ONE you'd actually post:\n\n{listing}", effort="high", max_tokens=500)
        s, e = out.find("{"), out.rfind("}")
        best = int(json.loads(out[s:e + 1]).get("best", 0))
        if 0 <= best < len(cands):
            return cands[best]
    except Exception:  # noqa: BLE001 — chooser must never break generation
        pass
    return cands[0]
