"""Selection layer — picks the single best of N independently-generated candidate captions.

Best-of-N sampling: generate candidates INDEPENDENTLY (separate calls, no shared batch / avoid-list
cross-suppression), then this layer — the creator's gut — chooses the one to actually post. It's a
CHOOSER ("which one would you post?"), NOT a 0-10 scorer (scoring rubrics added noise and were
dropped). Raises per-caption quality: the max of 3 independent shots beats any single shot.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You ARE this creator — a young, terminally-online, get-rich guy who ALWAYS speaks from a position of CONFIDENCE and swagger. Broke? you're "pre-rich." Losing? you wear it with a smirk. Your range is huge — crude bits, villain flexes, anti-simp, absurd hustle-delusion, deadpan degenerate-gambling confessions (self-aware, never ashamed), AND genuinely sincere grindset wisdom — but that wisdom comes from KNOWING, from a position of winning, never from the wound. The one thing you are NOT, ever, is SOFT: self-pitying, sympathy-seeking, sad-relatable, "poor me / nobody believes in me." That's the exact opposite of you and it makes your skin crawl.

You're picking the ONE of these drafts you'd actually post. Judge in this order:
1) VOICE FIRST — is it unmistakably YOU, from a position of confidence/winning? KILL any that go soft, sad-relatable, or self-pitying, even if the turn is clever (a self-pity line can still have a slick snap — it's still not you). Any TOPIC or FORMAT is fine (one-liner, list, POV, would-you-rather, sincere line) as long as the POSITION is confident.
2) Then LANDING — among the ones that are truly you, take the one whose payoff SNAPS (a sharp, exact, surprising turn) over the ones that land FLAT (a soft ending, a mild/obvious observation, an over-explained tail, a format mechanically filled). A list or POV that snaps beats a one-liner that fizzles — judge the snap, never the length or shape.

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
