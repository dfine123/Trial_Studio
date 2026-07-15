"""Selection layer — picks the single best of N independently-generated candidate captions.

Best-of-N sampling: generate candidates INDEPENDENTLY (separate calls, no shared batch / avoid-list
cross-suppression), then this layer — the creator's gut — chooses the one to actually post. It's a
CHOOSER ("which one would you post?"), NOT a 0-10 scorer (scoring rubrics added noise and were
dropped). Raises per-caption quality: the max of 3 independent shots beats any single shot.
"""
from __future__ import annotations

import json
import random

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

A line that's ALIVE beats a line that's WISE. A scene with a reply, a quote getting flipped, a behavior caught exactly, a flex held with a straight face — those get sent. A well-worded observation about life is a poster: it collects a nod and gets scrolled. If one option sounds like something a guy said and another sounds like something somebody wrote, the said one wins. Never pick a line BECAUSE it sounds smart — sounding smart is a warning sign, not a credential. (A sincere jab still counts as alive when it stings someone specific and turns at the end — wise-sounding is only disqualifying when nothing actually happens in the line.)

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
    # SHUFFLE before listing (2026-07-15 forensics): candidates arrive in fixed engine order
    # (screenshot always first), and every failure path below used to resolve to index 0 —
    # live picks were index-0 in 56/56 graded post-07-07 and 59/63 pending (13% before).
    # Shuffling makes position carry zero lane information, so residual primacy bias and any
    # fallback land on a random lane instead of silently always the same one.
    order = list(range(len(cands)))
    random.shuffle(order)
    listing = "\n\n".join(f"[{i}] {cands[j]}" for i, j in enumerate(order))
    # NOTE: the distilled-taste block was REMOVED here — it narrowed selection toward "tight one-twist" and
    # sanded the range (lists/POV/developed/sincere). Selection stays best-first + full-range.
    # max_tokens: adaptive thinking spends from the SAME budget (the documented lab truncation
    # bug) — at 500 the sonnet judge's JSON truncated and the silent except shipped cands[0];
    # that was the real cause of the post-07-06 index-0 monoculture. 3000 leaves thinking room.
    try:
        out = complete_json(_system(), f"Pick the ONE you'd actually post:\n\n{listing}", effort="high", max_tokens=3000,
                            cache_system=True, tag="chooser",   # stable system → cross-reel cache hits
                            model=getattr(settings, "chooser_model", None) or None)
        s, e = out.find("{"), out.rfind("}")
        if s == -1 or e == -1:
            raise ValueError(f"no JSON object in chooser output (len={len(out)}): {out[:120]!r}")
        best = int(json.loads(out[s:e + 1])["best"])
        if 0 <= best < len(cands):
            return cands[order[best]]
        raise ValueError(f"chooser index {best} out of range 0..{len(cands) - 1}")
    except Exception as exc:  # noqa: BLE001 — chooser must never break generation
        # LOUD fallback (the old bare `pass` hid 8 days of truncation): log why, and fall back
        # to the first LISTED candidate — post-shuffle that's a uniformly random lane, never a
        # structurally privileged one.
        print(f"[chooser] FALLBACK ({exc}) — shipping listed[0]", flush=True)
    return cands[order[0]]
