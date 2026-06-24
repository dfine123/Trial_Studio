"""Corpus-driven caption engine — section-based ensemble.

Each batch is composed of focused LANES, each the SAME reference-dominated generator grounded in
its own slice of the corpus — so adding a lane never degrades the others:
  - voice   : the approved funny / degen / villain / anti-simp voice (UNCHANGED).
  - serious : sincere grind-wisdom + sharp truths + anti-motivational subversions.
  - clip    : reaction / "when X but Y" captions that fit a SPECIFIC clip.
A simple section allocator picks how many of each per batch; lanes run in parallel; one editor
pass cleans the merged batch.
"""
from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs

# Traits that belong to the SERIOUS / motivational lane.
_SERIOUS_TRAITS = {
    "deep_bro_sincere", "deep_bro_provocative", "deep_bro_wisdom", "antimediocrity_dread",
    "anticope_callout", "sincere_mentor", "crude_motivational", "absurd_motivational",
    "money_mindset", "grindset_reassurance", "pro_grindset_sarcasm",
}

# Representative clip contexts for grading (the reel pipeline passes the real clip).
_SAMPLE_CLIPS = [
    "POV driving a flashy car through the city at golden hour",
    "lounging at a resort pool at night, on the phone",
    "candid at a fancy restaurant table, mid-bite, glancing at the camera",
    "walking up to an exotic car in a parking lot",
    "selfie in the car at night, deadpan",
    "shirtless gym / physique flex shot",
    "looking out a high-rise window over the city skyline",
]


def _is_clip_ref(r: dict) -> bool:
    if r.get("clip_dependency") in ("soft", "intrinsic"):
        return True
    cap = (r.get("caption") or "").lower()
    return cap.startswith(("pov", "when ", "how i look", "how bro", "me when", "me after", "what the"))


_GAMBLING_TERMS = (
    "parlay", "casino", "blackjack", "dealer", "slot", "sportsbook", "vegas", "lottery",
    "gambl", "on black", "on red", "the odds", "comp room", "referral code", "the under",
    "the over", "buzzer beater", "betting", "a bet", "day trad", "rimmed out", "put $",
)


def _is_gambling(r: dict) -> bool:
    if r.get("persona_trait") == "self_aware_degenerate":
        return True
    cap = (r.get("caption") or "").lower()
    return any(t in cap for t in _GAMBLING_TERMS)


# The structural palette actually present in the corpus. The model defaults to ~6 of these; we
# rotate a shuffled subset into each batch so variety widens within a batch and differs across them.
_FORMATS = [
    "a dead-simple one-liner",
    "a two-speaker bit (Mom: ... / Me: ...) or (Officer: ... / Me: ...)",
    "an enumerated list (bigger than you = roids / richer = daddy's money / ...)",
    "a fake statistic (98% have abs, 88% have a liquid mil)",
    "an X-is-like-Y analogy",
    "an absurd scheme or fake math (buy a chicken for $20, charge $8M an egg)",
    "a fake / misattributed quote gag (- MLK, probably)",
    "a flipped familiar phrase (objects in account are smaller than they appear)",
    "a POV: scenario",
    "a proverb subversion (a poor man X / a wise man Y)",
    "a when-X-but-Y reaction",
    "a she-said-X then subversion clapback",
    "an anti-motivational timeline (Zuckerberg at 19... it's over bro)",
    "backhanded encouragement (keep grinding bro, the world needs more ...)",
    "a self-own pushed to an absurd extreme",
    "a reframe (X is just Y with extra steps)",
]


_SYS_VOICE = """You write short-form captions AS ONE specific creator. Below are REAL captions of theirs — this IS the voice. Match it: the exact language and slang, the FORMATTING (line breaks, length), and their kind of humor (very-online, blunt, gambling/degenerate, crude, anti-motivational subversions).

- Their captions are SPECIFIC but CLEAN — usually ONE sharp detail, and the joke lands in a single beat. Do NOT stack multiple specifics or pile on jargon (parlay legs, point spreads, audit timelines) into a convoluted scenario — if a normal person can't get it in one quick read, it's overstuffed. Punchy beats elaborate; when in doubt, cut to the cleaner version.
- A recognizable TEMPLATE ("would you rather X or Y", etc.) only earns its place if THIS specific joke lands — a genuinely hard dilemma, OR a condition that's absurd/funny in itself. A limp twist no one would actually weigh is a DEAD riddle: not a real choice, not funny. Never fill a template just because it's a template.
- Match their FORMATTING: multi-line with line breaks when they do it, dead-simple one-liner when they do that. Lowercase-leaning. Footage is flexible flashy b-roll — don't write reaction captions that need a specific shot.
- Don't copy or reword any reference — fresh angles. Don't rehash any exact line in the AVOID list.

Return ONLY JSON, no prose:
{"candidates": [{"text": "the caption (\\n for line breaks)", "mode": "short label", "primary_lever": "shareability|comment_bait|relatability|iykyk_decode|shock_humor|...", "why": "one line"}]}"""

_SYS_SERIOUS = """You write short-form SERIOUS / motivational captions AS ONE specific creator. Below are REAL serious captions of theirs — this IS the voice for this lane: sincere grind-wisdom, sharp life-truths, and ANTI-motivational subversions ("Don't forget: Zuckerberg founded Facebook at 19... it's over bro"; "Winners lose more than losers ever will"; "nobody respects the boring years").

- SHARP and REAL — a hard truth or a clean reframe that lands in one beat. NEVER corny, NEVER a poster-metaphor (no "the seed doesn't argue with the dirt", no "the river carves the canyon"), NEVER soft or wistful.
- The subversive ones (looks like motivation, then undercuts it) are gold. lowercase-leaning, very-online; match their formatting.
- Don't copy or reword any reference — fresh angles. Don't rehash any exact line in the AVOID list.

Return ONLY JSON, no prose:
{"candidates": [{"text": "the caption (\\n for line breaks)", "mode": "short label", "primary_lever": "shareability|comment_bait|relatability|...", "why": "one line"}]}"""

_SYS_CLIP = """You write short-form captions that sit OVER A SPECIFIC VIDEO CLIP, AS ONE specific creator. The caption reacts to or plays off what's ON SCREEN. Below are REAL clip-style captions of theirs (reaction, "when X but Y", "how I look at X after Y", "POV") — match that style.

- The caption MUST connect to the footage described: a reaction to it, a "when [the on-screen situation] but [twist]", or a POV that fits the shot. If it would work over any random video, it's not clip-aware enough.
- clean, one beat, very-online, blunt, not corny. match their formatting.
- Don't copy or reword any reference — fresh angles. Don't rehash any exact line in the AVOID list.

Return ONLY JSON, no prose:
{"candidates": [{"text": "the caption (\\n for line breaks)", "mode": "short label", "primary_lever": "shareability|comment_bait|relatability|...", "why": "one line"}]}"""


def _gold_block(refs: list[dict], k: int) -> str:
    # Faithful random sample — the references ARE the voice (degenerate core included). Do NOT
    # cap or rebalance them: an under-representative sample is exactly what drifts the model toward
    # generic grindset/flex and away from the references.
    pool = list(refs)
    random.shuffle(pool)
    gold = [(r.get("caption") or "").strip() for r in pool[:k] if (r.get("caption") or "").strip()]
    return "\n\n".join(f"[{i + 1}]\n{c}" for i, c in enumerate(gold)) or "(none)"


def _lane(sys: str, gold_block: str, n: int, avoid_block: str, audio_vibe, audio_energy, notes, extra: str = "") -> list[dict]:
    user = (
        f"REAL CAPTIONS FROM THIS CREATOR — THIS is the voice; write new ones that could sit in this list unnoticed:\n\n"
        f"{gold_block}\n\n"
        f"RECENTLY SHOWN — don't rehash these exact lines (a fresh joke on a similar setup is fine):\n{avoid_block}\n\n"
        f"{extra}"
        f"Audio vibe: {audio_vibe or 'n/a'} ({audio_energy or ''}). Notes: {notes or 'none'}.\n"
        f"Write {n} new captions in this voice. No two alike. Keep each CLEAN and punchy — ONE sharp idea that lands "
        f"in a single beat, not a pile of stacked specifics or jargon. Match their formatting."
    )
    text = complete_json(sys, user, effort="high", max_tokens=3000)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start : end + 1]).get("candidates", [])[:n]
    except json.JSONDecodeError:
        return []


def _tag(cands: list[dict], lane: str) -> list[dict]:
    for c in cands:
        c["lane"] = lane
    return cands


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Compose a batch from focused lanes (voice + serious + clip), run them in parallel, merge,
    then run one editor pass over the whole batch."""
    refs = load_refs()
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"

    serious_refs = [r for r in refs if r.get("persona_trait") in _SERIOUS_TRAITS] or refs
    clip_refs = [r for r in refs if _is_clip_ref(r)] or refs
    clip = clip_context or random.choice(_SAMPLE_CLIPS)

    # Section allocation: mostly voice, always some serious + one clip-aware. Lean more serious
    # on slow/reflective audio.
    n_clip = 1 if n >= 6 else 0
    reflective = (audio_energy == "low") or bool(audio_purpose and "reflective_glowup" in audio_purpose)
    n_serious = min(n - n_clip - 1, 3 if reflective else 2)
    n_voice = max(1, n - n_serious - n_clip)

    # REFERENCE-DOMINATED: the gold block above IS the voice. One light note keeps range without
    # forcing templates, and reasserts the unhinged core that generic drift erodes.
    topic_note = (
        "Stay locked to the references' voice — their exact slang, their formatting, their unhinged hyper-specific "
        "humor (gambling/degenerate, crude, anti-motivational subversions are CORE — never sand them into something "
        "safe). Range across setups so they're not all alike, but NEVER settle for a generic grindset / flex / "
        "fake-stat line a normal money account would post — if it isn't genuinely funny the way THESE references are, "
        "throw it out.\n\n"
    )

    def voice():
        return _tag(_lane(_SYS_VOICE, _gold_block(refs, 40), n_voice, avoid, audio_vibe, audio_energy, notes, topic_note), "voice")

    serious_note = (
        "Match the references' SERIOUS lines — a sharp real truth or clean reframe in one beat, never a corny "
        "poster-metaphor or a generic motivational line. Vary the angle; only keep it if it genuinely lands.\n\n"
    )

    def serious():
        return _tag(_lane(_SYS_SERIOUS, _gold_block(serious_refs, 22), n_serious, avoid, audio_vibe, audio_energy, notes, serious_note), "serious")

    def clipaware():
        if not n_clip:
            return []
        extra = (
            f"THE CLIP this caption sits over: {clip}. The caption must connect to / react to what's on screen "
            "(react to THIS shot — a blunt or funny reaction; don't default to a casino/gambling joke or a wistful "
            "'nobody sees the real me' reflection).\n\n"
        )
        return _tag(_lane(_SYS_CLIP, _gold_block(clip_refs, 18), n_clip, avoid, audio_vibe, audio_energy, notes, extra), "clip")

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(voice), ex.submit(serious), ex.submit(clipaware)]
        cands: list[dict] = []
        for f in futures:
            try:
                cands += f.result() or []
            except Exception:  # noqa: BLE001 — one lane failing shouldn't kill the batch
                pass

    cands = refine(cands)
    log_generated([c.get("text", "") for c in cands])
    return cands
