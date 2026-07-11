"""Selection layer — picks the single best of N independently-generated candidate captions.

Best-of-N sampling: generate candidates INDEPENDENTLY (separate calls, no shared batch / avoid-list
cross-suppression), then this layer — the creator's gut — chooses the one to actually post. It's a
CHOOSER ("which one would you post?"), NOT a 0-10 scorer (scoring rubrics added noise and were
dropped). Raises per-caption quality: the max of 3 independent shots beats any single shot.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

# The chooser is BEST-CAPTION-FIRST. Voice/personality is MODULAR: the per-profile persona (persona.md —
# the same file generation reads) is injected at call time, and the only voice role is a VETO on clearly
# unaligned drafts. The shared text below is the BASE all profiles largely stem from (confident position,
# never soft/self-pity); everything profile-specific (topics, flavors) lives in the persona, not here.
_PICK_HEAD = """You ARE this creator, picking the ONE of your own draft captions you'd actually post.

WHO YOU ARE:
"""

_PICK_TAIL = """

PICK THE BEST CAPTION — that is the whole job: the one that lands hardest read cold, the one a guy would actually screenshot or send. Judge it like a reader, not a writer:

A line that's ALIVE beats a line that's WISE. A scene with a reply, a quote getting flipped, a behavior caught exactly, a flex held with a straight face — those get sent. A well-worded observation about life, however true, is a poster: it collects a nod and gets scrolled. If one option sounds like something a guy said and another sounds like something somebody wrote, the said one wins. Never pick a line BECAUSE it sounds smart — sounding smart is a warning sign here, not a credential.

A payoff that SNAPS (a sharp, exact, surprising turn the reader finishes himself) beats one that lands FLAT (a soft ending, a mild observation, an over-explained tail). Any format can win — dialogue, list, one-liner, would-you-rather, sincere jab — judge the landing, never the length or the shape.

The ONE veto: skip a draft that's clearly not you — it reads soft, self-pitying, or sympathy-seeking, or plainly clashes with who you are above — even if its turn is clever. Among everything that IS you, the best caption wins, period.

Return ONLY JSON, no prose: {"best": <0-based index of the single best caption>}"""


def _system() -> str:
    """Best-first judging around the ACTIVE profile's persona (modular — swaps with the profile)."""
    from app.caption.engine import persona   # lazy: the per-profile embodiment generation also uses
    return _PICK_HEAD + persona() + _PICK_TAIL


def choose_best(candidates: list[str]) -> str:
    """Return the single best caption to post. Falls back to the first on any error.

    ⚠️ The judge MODEL is settings.chooser_model (sonnet-4-6), NOT the generation model. Measured
    on the frozen 22-case correction eval (2026-07-06): opus-as-judge re-picked the operator-
    REJECTED caption 17/22 — a systematic attraction to clever-SOUNDING lines ("chooser-bait")
    that FIVE prompt variants (literal-read, ordered judging, few-shot corrections, bias
    counterweight, audience frame) failed to move; swapping the judge to sonnet dropped
    loser-picks to 2/22 (below the 4.4 chance rate) with 6/22 correct, identical prompt.
    Any future chooser change must beat that on the HOLDOUT half of the frozen set."""
    from app.config import settings
    cands = [c for c in candidates if (c or "").strip()]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    listing = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(cands))
    # NOTE: the distilled-taste block was REMOVED here — it narrowed selection toward "tight one-twist" and
    # sanded the range (lists/POV/developed/sincere). Selection stays best-first + full-range.
    try:
        out = complete_json(_system(), f"Pick the ONE you'd actually post:\n\n{listing}", effort="high", max_tokens=500,
                            cache_system=True, tag="chooser",   # stable system → cross-reel cache hits
                            model=getattr(settings, "chooser_model", None) or None)
        s, e = out.find("{"), out.rfind("}")
        best = int(json.loads(out[s:e + 1]).get("best", 0))
        if 0 <= best < len(cands):
            return cands[best]
    except Exception:  # noqa: BLE001 — chooser must never break generation
        pass
    return cands[0]
