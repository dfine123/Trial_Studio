"""Caption engine — reference-dominated, full-range, ROTATION-anchored + GRADE-WEIGHTED (closed loop).

Voice: a young terminally-online get-rich guy, raw money slang, allergic to corporate/poetic.
Coverage: each batch slot is anchored to a DISTINCT real reference, rotated least-used-first through
the whole corpus so every FORMAT gets covered and nothing repeats until cycled.

CLOSED LOOP (the "naturally avoid / naturally amplify" layer): every graded caption is attributed
back to its anchor reference (app/corpus/attribute.py -> per-profile ref_scores.json: per-ref
keep/kill/best, in-process + exact). `_pick_anchors` then weights the rotation by that signal — chronically-killed
formats (e.g. crime-term wordplay the user keeps killing "gay") drop OUT of rotation, proven winners
recur more. The user's grading reshapes which formats the engine reaches for. No in-context kill-
steering (selection only), no hard-coded format bans, no judge. Falls back to pure rotation when
scores are absent.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor

from app import profiles
from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs


def _drop_ref_copies(cands: list[dict]) -> list[dict]:
    """Drop candidates that regurgitate or NOUN-SWAP a corpus reference (the catalog is the voice,
    never the output). Two tiers, both mechanical curation (never a prompt rule):
      - verbatim/near-verbatim copies: word containment ≥ .8 on the raw text (round-2 grading:
        3 of 13 'winners' were corpus copies);
      - MORPHS: containment ≥ .62 on MARKER-STRIPPED content ("Seagulls don't got a resume…" vs
        the pigeons ref; "tied off my bloodline" vs r003 — the operator's 'mashed together'
        class, 2026-07-07). Stripping the frame openers first keeps frame SPECIES legitimate:
        a fresh would-you-rather / "dudes be like" shares its skeleton words by design and is
        compared only on its content.
    If everything got dropped (pathological), keep the originals rather than return nothing."""
    from app.corpus.promote import _too_similar
    strip_markers = _FRAME_MARKERS + ("dudes be like ", "keep grinding bro", "keep grinding,")

    def content(t: str) -> str:
        t = (t or "").replace("\n", " / ").strip()
        while t and t[0] == "🥷":
            t = t.lstrip("🥷").lstrip("s'’ ").strip()
        low = t.lower()
        for m in strip_markers:
            if low.startswith(m):
                return t[len(m):].lstrip(" :—-\"'").strip()
        return t

    refs = [(r.get("caption") or "") for r in load_refs() if (r.get("caption") or "").strip()]
    try:    # NORTH STARS are exemplars too — with a thin corpus they become super-attractors
            # (2026-07-09: a fresh profile noun-swapped the hater-lottery star into "mortgage")
        from app.caption import northstars
        refs += [(r.get("caption") or "") for r in northstars.load() if (r.get("caption") or "").strip()]
    except Exception:  # noqa: BLE001
        pass
    kept = [c for c in cands
            if not any(_too_similar(c.get("text") or "", t)
                       or _too_similar(content(c.get("text") or ""), content(t), thr=0.62)
                       for t in refs)]
    return kept or cands


_GATE_SYS = """You check short captions for ONE thing only: INTERNAL MECHANICAL COHERENCE. Grant every premise, no matter how absurd, crude, or exaggerated. Inside its own premise, does the line's mechanism resolve — do the numbers compute, do the comparisons map one-to-one, do the referents stay consistent, does the payoff connect to its setup, and does the caption hand the reader everything it needs to parse it? Flag ONLY a line whose mechanism contradicts ITSELF or whose payoff does not parse on a literal read. NEVER flag for taste, style, absurdity, edginess, hyperbole, slang, or not being funny — a metaphor that maps is coherent; an absurd-but-airtight leap is coherent.

Return ONLY JSON: {"broken": [0-based indices of mechanically broken lines]}"""


def check_coherence(texts: list[str]) -> list[int]:
    """Indices of captions whose internal mechanism BREAKS on a literal read (grant-the-premise —
    absurdity is never flagged, only self-contradiction). Best-effort: [] on any error."""
    if not texts:
        return []
    lines = "\n".join(f"[{i}] {(t or '').strip()}" for i, t in enumerate(texts)).replace("\n\n", "\n")
    try:
        out = complete_json(_GATE_SYS, lines, effort="low", max_tokens=1500, tag="gate")
        s, e = out.find("{"), out.rfind("}")
        broken = json.loads(out[s:e + 1]).get("broken", []) if s != -1 else []
        return sorted({int(i) for i in broken if isinstance(i, (int, float)) and 0 <= int(i) < len(texts)})
    except Exception:  # noqa: BLE001
        return []


def _coherence_gate(cands: list[dict]) -> list[dict]:
    """COHERENCE GATE — subtractive curation (same class as _drop_ref_copies/refine, never a prompt
    rule): drops candidates whose joke MECHANISM breaks on a literal read — round-3's dominant kill
    driver (~9 of 18 kills: "18% tip = 18 grown men", "bank does 3 to 0"). ⚠️ MEASURED NEGATIVE,
    default OFF: replayed against round 3 itself (kills vs hits vs endorsed vs corpus), two prompt
    framings both scored recall 0/9 at clean precision — a joke-charitable judge PARSES these lines
    fine; the operator's objection is sloppy MAPPING (taste-grade), and strictness high enough to
    catch it is the distilled-taste-filter failure shape (flags paradox/absurdist refs first). The
    class is addressed at generation instead (PRECISION literal-read grounding in _MECHANICS).
    Failsafes if ever enabled: LLM error / >half flagged / <2 survivors -> keep all. Mode via
    settings.coherence_gate: 'off' (default) | 'log' | 'drop'; harness: /api/debug/gate-check."""
    from app.config import settings
    mode = (getattr(settings, "coherence_gate", "log") or "log").lower()
    if mode == "off" or len(cands) < 2:
        return cands
    flagged = check_coherence([c.get("text") or "" for c in cands])
    if flagged:
        shown = " ".join(f"[{i}:{(cands[i].get('text') or '')[:60]!r}]" for i in flagged)
        print(f"[gate] mode={mode} flagged={len(flagged)}/{len(cands)} {shown}", flush=True)
    if mode != "drop" or not flagged:
        return cands
    if len(flagged) > len(cands) / 2:
        print("[gate] over-flagged -> keep all", flush=True)
        return cands
    kept = [c for i, c in enumerate(cands) if i not in set(flagged)]
    return kept if len(kept) >= 2 else cands


_FRAME_MARKERS = ("pov:", "pov ", "would you rather ", "wtf is ", "when ", "how bro ")


_OPENER_MARKERS = ("mfs will ", "mfs keep ", "mfs call ", "mfs ", "broke dudes ", "dudes be like ",
                   "everybody ", "a girl who ", "bro will ", "bro ")


def _avoid_stub(c: str, stub_words: int = 9) -> str:
    """One line's premise stub: FORMAT/OPENER MARKERS are stripped FIRST, then the first words of
    the CONTENT are taken — so the avoid list describes used IDEAS, never used openers. ⚠️ Two
    regressions this fixes: raw first-9-word stubs put the format marker itself in the list
    ("POV: …" ×30) under a "don't rehash" instruction — suppressing VALIDATED species; and a wall
    of same-opener stubs ("mfs will …" ×50) visually PRIMES that opener as 'what my output looks
    like' (the 2026-07-08 mfs scaffold-lock: 7/10 in one batch)."""
    t = (c or "").replace("\n", " / ").strip()
    while t and t[0] == "🥷":
        t = t.lstrip("🥷").lstrip("s'’ ").strip()
    low = t.lower()
    for m in _FRAME_MARKERS + _OPENER_MARKERS:
        if low.startswith(m):
            t = t[len(m):].lstrip(" :—-").strip()
            break
    ws = t.split()
    return " ".join(ws[:stub_words]) + ("…" if len(ws) > stub_words else "")


def _avoid_block(window: int = 150, stub_words: int = 9) -> str:
    """The anti-repeat list as PREMISE STUBS (marker-stripped content openers), not full captions.

    Premise anti-repeat only: full texts acted as 150 in-prompt length examples (measured ratchet,
    pool 17.5 -> 19.9 words) and raw opener-stubs read as banned FORMATS (measured species loss).
    Content stubs carry the idea signal with zero length signal and zero format signal."""
    stubs = [_avoid_stub(c, stub_words) for c in recent_generated(window)]
    return "\n".join("- " + s for s in dict.fromkeys(s for s in stubs if s)) or "(none yet)"

_GAMBLING_TERMS = (
    "parlay", "casino", "blackjack", "dealer", "slot", "sportsbook", "vegas", "lottery",
    "gambl", "on black", "on red", "the odds", "comp room", "referral code", "the under",
    "the over", "betting", "a bet", "rimmed out", "put $", "down bad on this hand", "the hand",
    "card declined", "deposit", "hit me", "hitting is", "ante", "roulette", "scratch off", "scratch ticket",
)
# ref usage/scores are voice files -> resolved per ACTIVE PROFILE via app.profiles (not global)


def _is_gambling(r: dict) -> bool:
    if r.get("persona_trait") == "self_aware_degenerate":
        return True
    cap = (r.get("caption") or "").lower()
    return any(t in cap for t in _GAMBLING_TERMS)


def _ref_key(r: dict) -> str:
    return r.get("ref_id") or (r.get("caption") or "")[:60]


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_ref_usage(usage: dict) -> None:
    path = profiles.ref_usage_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(usage, f)
    os.replace(tmp, path)


# The VOICE has two layers. The shared FORMAT base (bridge + mechanics below) is the SAME for every
# profile — it's the core that makes any caption (and the template format) land. The PERSONA (who this
# creator IS) is PER-PROFILE: read from the active profile's persona.md, with the profile's own corpus
# as the references. So a new creator gets the same format base IN THEIR voice — never Spence's.
_BRIDGE = "\n\nBelow are your REAL captions — this is the voice, the range, AND the bar:\n\n{references}\n\n"

_MECHANICS = """What every one of your captions shares — the FORMAT instincts (feel them, don't check them off):
- THE TWIST. The setup primes one read; the line flips to another — the GAP is the joke. It can be a decode, a reframe, a bait-and-switch, or a self-own — but the whole line exists to land that turn.
- PRECISION. The twist maps EXACTLY — the two halves line up perfectly. Approximate or almost-funny is dead. Grant yourself any absurd premise, but inside it the mechanism must survive a LITERAL read: numbers compute, comparisons map one-to-one, the payoff follows from its setup, and the line hands the reader everything it needs to parse it. The strongest payoffs cash a TRUE double meaning — the literal read and the figurative/slang read both land.
- ECONOMY. The hit lands in the fewest words that carry it — one clean move, then stop. You trust the reader to get it: never explain the joke, pad the setup, or tack on a second and third payoff. Most of your best lines are dead-simple; length is earned ONLY when every beat does real work. If a line can be cut and still hits, it was too long.
- DEADPAN CONFIDENCE. Stated flat, like it's obvious, even when it's unhinged.
- HYPER-SPECIFIC + VERY-ONLINE. Real specifics — named things, real numbers, real slang, emoji when it lands — never vague. The specifics that hit hardest live in THIS creator's world: the exact props, apps, and characters the references breathe.
- ALWAYS SHARP — never generic, never corporate, never a motivational poster. Even a sincere line is a SPECIFIC truth or a parody, never a platitude."""

_DEFAULT_PERSONA = """You ARE this creator. The captions below are your real posts — your voice, your range, and the bar. Write only in that exact voice: the same register, slang, rhythm, and attitude. Never corporate, poetic, or generic."""


def persona() -> str:
    """The ACTIVE profile's authored persona embodiment (who this creator IS), or a neutral default."""
    try:
        with open(profiles.persona_path(), encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_PERSONA


def voice_system(ref_block: str) -> str:
    """Compose the system prompt: per-profile PERSONA + the profile's references + the shared FORMAT base.
    Generation stays reference-DOMINATED — the references carry the voice + its full range. Learned taste
    (incl. off-voice) belongs in the CHOOSER (selection), never as rules/negative examples in generation."""
    return persona() + _BRIDGE.format(references=ref_block) + _MECHANICS


_SINCERE_TRAITS = ("sincere", "grindset", "mindset", "wisdom", "motivational")


def _ref_species(r: dict) -> str:
    """Coarse species for the rotation floor: 'frame' (marker-opened formats), 'sincere'
    (real-talk/proverb traits), else 'other'."""
    cap = (r.get("caption") or "").strip()
    if cap.startswith("🥷") or cap.lower().startswith(_FRAME_MARKERS):
        return "frame"
    trait = (r.get("persona_trait") or "").lower()
    if any(t in trait for t in _SINCERE_TRAITS):
        return "sincere"
    return "other"


def _quality_offsets(refs: list[dict]) -> dict[str, int]:
    """PRODUCE-mode virtual-usage offsets from the signal that actually exists in the reel era:
    each graded reel rates its POSTED caption, attributed to that caption's anchor. Per ref:
    the LAST 5 posted ratings (staleness cap — ancient history can't freeze a penalty) + batch
    keep/kill folded in as weak pseudo-ratings at half weight (the batch-grading surface is the
    rehabilitation path: a delayed ref can clear its penalty through batch keeps without ever
    anchoring a reel). shrunk = (Σ + 5μ)/(n + 5) — heavy shrinkage because a reel rating grades
    caption+clips+audio jointly; offset = clamp(round((μ − shrunk)·2), −3, +3), the canon-blessed
    failer magnitude. Strong refs rotate SOONER, weak-history refs LATER, nothing is ever excluded.
    ⚠️ Adversary-reviewed (2026-07-06): NO provenance pseudo-observations — measured on 240 slates,
    validated-ANCHORED posted reels mean 4.98 (below μ 5.29) vs graded seeds 6.71: an operator-loved
    LINE is not a fertile ANCHOR; refs earn their tier from data only. Best-effort: {} on error."""
    try:
        from app.corpus import reels as reel_store
        per_ref: dict[str, list[int]] = {}
        ratings: list[int] = []
        for r in reel_store.graded():
            rating = (r.get("grade") or {}).get("rating") or 0
            if not rating:
                continue
            ratings.append(rating)
            for rid in (r.get("caption_anchor_refs") or []):
                if rid:
                    per_ref.setdefault(rid, []).append(rating)
        if len(ratings) < 20:   # not enough signal — neutral offsets (pure rotation, old behavior)
            return {}
        mu = sum(ratings) / len(ratings)
        scores = _load_json(profiles.ref_scores_path())
        out: dict[str, int] = {}
        for ref in refs:
            rid = ref.get("ref_id") or ""
            recent = per_ref.get(rid, [])[-5:]
            s, c = float(sum(recent)), float(len(recent))
            sc = scores.get(rid, {})
            k, x = min(sc.get("keep", 0), 4), min(sc.get("kill", 0), 4)
            s += (mu + 2) * 0.5 * k + (mu - 2) * 0.5 * x
            c += 0.5 * (k + x)
            shrunk = (s + 5 * mu) / (c + 5)
            out[rid] = max(-3, min(3, round((mu - shrunk) * 2)))
        return out
    except Exception:  # noqa: BLE001 — quality weighting must never break generation
        return {}


_USAGE_LOCK = threading.Lock()   # anchor selection is read-modify-write on ref_usage.json — batch
                                 # generation runs reels concurrently, and racing pickers would both
                                 # see the same least-used view (duplicate anchors) and lose updates


def _pick_anchors(refs: list[dict], n: int, produce: bool = False) -> list[dict]:
    """n DISTINCT reference anchors. Two modes:
    - EXPLORE (default; the batch-grading path): least-used-first coverage rotation + winner
      reserve + SPECIES FLOOR — the operator's learning surface, unchanged.
    - PRODUCE (the reel path): the same reserve + floor + trait/gambling rules, PLUS quality-
      weighted rotation via _quality_offsets. Slate forensics (2026-07-06, 240 graded slates):
      the winner reserve was a structural NO-OP in the reel era (is_winner needed ≥6 keep/kill
      credits; only ≥8-posted keeps exist — amplified=[] live, 240/240 slates zero winners), so
      every production slot was pure coverage — the operator's "the alternates are always 1-3,
      as if generated with the intention to not be selected". The fix: the is_winner era-fix
      revives the reserve (the persistent amplifier) in both modes, and produce mode adds the
      posted-rating offsets (entry phasing: strong sooner, weak-history later). Nothing is
      excluded: combined virtual usage clamped ±3 = a few cycles' delay at most."""
    usage = _load_json(profiles.ref_usage_path())
    scores = _load_json(profiles.ref_scores_path())

    def _stat(r: dict) -> tuple[int, int, int]:
        s = scores.get(r.get("ref_id") or "", {})
        return s.get("keep", 0), s.get("kill", 0), s.get("best", 0)

    def is_failer(r: dict) -> bool:  # chronic low-keep-rate -> DE-WEIGHT (rotate in later/rarer), NEVER drop.
        # A miss is evidence about an EXECUTION, not a verdict on the format (operator's standing rule) —
        # grading must never shrink the range, so every ref stays in rotation.
        k, x, b = _stat(r)
        rate = (k + b) / (k + x) if (k + x) else 1.0
        return rate < 0.25 and x >= 4 and x > k + 3  # genuinely killed most of the time, with volume

    def is_winner(r: dict) -> bool:  # proven high-keep-rate format -> recur sooner
        # era-fix (2026-07-06): the reel era writes ONLY keeps (≥8 posted; the kill path was
        # removed by design), so the old (k+x)>=6 volume gate was unreachable — amplified=[] live,
        # 240/240 graded slates carried ZERO winner anchors: the amplify loop was structurally
        # dead. Two validated grades at ≥60% keep-rate now qualify; the pool grows with every ≥8.
        k, x, b = _stat(r)
        rate = (k + b) / (k + x) if (k + x) else 0.0
        return (k + x) >= 2 and rate >= 0.6

    healthy = [r for r in refs if (r.get("caption") or "").strip()]
    random.shuffle(healthy)  # random tiebreak among equally-used
    qoff = _quality_offsets(healthy) if produce else {}
    # least-used first; failers carry a virtual-usage penalty so they cycle less often but always
    # return; produce mode adds the quality offset (strong sooner, weak-history later, never out).
    # The COMBINED virtual usage is clamped to ±3 — a chronic failer with bad posted history must
    # not stack to +6/+7 (that becomes a de-facto drop, which canon rule 2 forbids).
    by_usage = sorted(healthy, key=lambda r: usage.get(_ref_key(r), 0)
                      + max(-3, min(3, (3 if is_failer(r) else 0)
                                    + qoff.get(r.get("ref_id") or "", 0))))

    anchors: list[dict] = []
    seen_traits: set[str] = set()
    gambling = [0]
    # gambling anchors scale with batch size (~corpus share, 10%) — a flat 2 allowed 40% on best-of-5
    gambling_cap = 1 if n <= 6 else 2

    def try_add(r: dict) -> None:
        if len(anchors) >= n or (r.get("persona_trait") or "?") in seen_traits:
            return
        if _is_gambling(r):
            if gambling[0] >= gambling_cap:
                return
            gambling[0] += 1
        anchors.append(r)
        seen_traits.add(r.get("persona_trait") or "?")

    # reserve ~2 slots for proven WINNERS (amplify), least-used winner first so they still rotate.
    # This is the codebase's only PERSISTENT amplifier (a quality offset in a least-used rotation
    # is just an entry phase-shift — equal steady-state frequency); it applies in BOTH modes and
    # is alive again after the is_winner era-fix above.
    n_win = min(2, max(1, n // 4))
    for r in by_usage:
        if len(anchors) >= n_win:
            break
        if is_winner(r):
            try_add(r)
    # SPECIES FLOOR (operator rule: validated species must never just disappear from batches) —
    # every batch of n≥5 carries at least one FRAME-format anchor (POV / 🥷 / would-you-rather /
    # wtf-is / when / how-bro — the frame-species exception then keeps it a frame in output) and
    # one SINCERE anchor (the largest seed cluster, structurally diluted by joke-heavy promotions:
    # 17 seeds vs 2/47 promoted — measured 2026-07-04). APPLIES IN PRODUCE MODE TOO
    # (adversary-reviewed 2026-07-06): sincere is the TOP-performing posted species (6.50 mean,
    # 41.7% ≥8 vs 5.20/18.3% for other) and species refs have little posted history — without the
    # floor the quality offsets would phase them out of reels, starving the learn loop. In produce
    # mode the floor slots fill by usage+offset order WITHIN each species (by_usage is already
    # offset-sorted), which removes the floor's one defect: quality-blind picks.
    if n >= 5:
        for want in ("frame", "sincere"):
            if not any(_ref_species(a) == want for a in anchors):
                for r in by_usage:
                    if _ref_species(r) == want:
                        try_add(r)
                        if any(_ref_species(a) == want for a in anchors):
                            break
    # fill the rest from the general least-used rotation (coverage + variety)
    for r in by_usage:
        if len(anchors) >= n:
            break
        try_add(r)
    if len(anchors) < n:  # ran out of distinct traits — relax
        chosen = {id(a) for a in anchors}
        for r in by_usage:
            if len(anchors) >= n:
                break
            if id(r) not in chosen:
                anchors.append(r)
                chosen.add(id(r))
    for r in anchors:
        usage[_ref_key(r)] = usage.get(_ref_key(r), 0) + 1
    _save_ref_usage(usage)
    random.shuffle(anchors)
    return anchors[:n]


def _anchor_render(label: str, a: dict) -> str:
    """Show the real caption AND the creator's own 'why it works' (the mechanism) so generation
    transposes the MECHANISM to a fresh subject instead of re-skinning the surface sentence. Falls
    back to caption-only when a ref has no why_it_works (e.g. a bootstrapped corpus)."""
    cap = (a.get("caption") or "").strip()
    why = (a.get("why_it_works") or "").strip()
    return f"{label}: {cap}" + (f"\n   WHY IT LANDS: {why}" if why else "")


def _cid(text: str) -> str:
    """Stable content-id for a caption (provenance + dedup): first 12 hex of sha1(text)."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


# ---- CRAFT-DEEPENED GROUNDING (A/B, off by default) ----
# Raise the aim WITHOUT a transform layer: each anchor also teaches THE CRAFT of its landing (the exact
# execution move), so generation aims at 9-craft per format instead of the 7-average. Grounding, not a
# post-process — the model still invents freely, so the range/voice is preserved. Gated by a ContextVar
# so production (off) is byte-for-byte unchanged.
_CRAFT: contextvars.ContextVar[bool] = contextvars.ContextVar("craft_grounding", default=False)

_CRAFT_SYS = """You are studying ONE of a creator's sharp captions to name the CRAFT of its landing — the specific execution move that makes THIS exact line hit so hard. NOT a generic virtue. It is faithful to THIS line, and it varies wildly line to line: a hyper-exact word or number, a concrete physical image, an absurd-but-airtight logical leap, a rhythm/cadence, a taboo edge, one precise real-world detail, or a genuinely true thing nobody says out loud. Name the ACTUAL move in this line — one tight sentence, concrete, never generic 'be specific' advice.

Return ONLY JSON: {"craft": "<the move that makes THIS line land>"}"""


def _anchor_craft(anchor: dict) -> str:
    """The execution craft of ONE anchor (what makes its landing hit). Best-effort; '' on error."""
    cap = (anchor.get("caption") or "").strip()
    if not cap:
        return ""
    why = (anchor.get("why_it_works") or "").strip()
    try:
        out = complete_json(_CRAFT_SYS, f"CAPTION:\n{cap}\n\nWHY IT LANDS: {why}", effort="low", max_tokens=200)
        s, e = out.find("{"), out.rfind("}")
        return (json.loads(out[s:e + 1]).get("craft") or "").strip() if s != -1 else ""
    except Exception:  # noqa: BLE001
        return ""


def _render_anchors(anchors: list[dict]) -> str:
    """The anchor block. In craft mode each anchor also carries THE CRAFT of its landing (computed in
    parallel, faithfully per-anchor so it raises the aim without collapsing to one 'be specific' center)."""
    renders = [_anchor_render(f"ANCHOR {i + 1}", a) for i, a in enumerate(anchors)]
    if _CRAFT.get():
        with ThreadPoolExecutor(max_workers=max(1, len(anchors))) as ex:
            crafts = list(ex.map(_anchor_craft, anchors))
        renders = [r + (f"\nTHE CRAFT: {c}" if c else "") for r, c in zip(renders, crafts)]
    return "\n\n".join(renders)


# ── THE VOICE CORE (operator-owned; replaces the accreted craft mechanics in v2 paths) ──
# Derived 2026-07-08 from the operator's north-star references + catalog + every grade. Stored on
# the volume (var/voice_core.md, GET/POST /api/voice-core) so the OPERATOR can edit the system's
# taste directly; this constant is only the seed/fallback.
_VOICE_CORE_DEFAULT = """What separates your bangers from your dead ones — the one line, from your own grades:

CONCRETE, NEVER ABSTRACT. Your 10s are always something you can SEE, or a specific character doing a specific thing: raccoons eating every single night, a 50-year-old genuinely hyped explaining his 401k match, the hater losing his mind when the vending machine drops two bags of chips, a guy proud he's in debt from a car that hits 60 in 3 seconds, flying bro out after you hit a million to ask if that "no homo" in 2019 was real. Your 1s are almost always an abstract DEFINITION of a concept — "an alarm clock is just your boss waking you up for free," "a job interview is just begging with better posture," "a resume is just a list of everyone you made rich except yourself." The "X is just Y" reframe FEELS clever but it's a tweet about a concept, not you being a menace. It is the single most common shape in your dead pile. Don't write it.

SPECIFIC AND FROM YOUR WORLD. The detail that lands is exact and yours — the Rothschilds (not "rich people"), an LED sign with your name on it (not "expensive stuff"), a $997 course on how to sell a $997 course, "no homo back in 2019." Vague dies ("equity," "my first deal"). Random-but-not-yours dies ("name embroidered on my gym towel"). It has to be a thing from your life: loud money, the come-up, degen gambling, spectacle, bro. And the LAST beat has to land on a concrete image — never a soft summary word ("with better posture", "a diagnosis", "some days just take longer to load" all die there).

DELUSIONAL CONFIDENCE. You never complain or explain. You flex the dumb thing like it's superior ("we in stealth mode," "i'm tryna nut twice before noon"), roast someone dead-on ("she believed in me when nobody else did / bc nobody else was that dumb"), or state something unhinged completely straight (edging taught you more about delayed gratification than any finance guru). Worn with a smirk, never seeking sympathy.

SAID, NOT WRITTEN. It reads like a thought you threw away — no setup ceremony, no punchline architecture, nothing that winks at you. The funniest guy in the room doesn't perform; he just says it.

THE READER FINISHES IT. Under-explain. Recognition ("this is so him"), a decode, hidden math — the reader supplies the laugh. The caption never laughs at itself.

THE SHAPES you actually run (all of them concrete, none of them abstract definitions): a specific scene ("she asks X, i can't say Y so i say Z"), a "when my phone buzzes…" moment, "Dudes be like '[quote]' / [brutal turn]", a "we are not the same" flex with a vivid self-own, an animal/hater held up as anti-cope, absurd-math schemes, would-you-rather, a POV, a quote and its comeback."""


def voice_core() -> str:
    """The operator-editable taste core (volume file; falls back to the seed above)."""
    try:
        with open(os.path.join("var", "voice_core.md"), encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    return _VOICE_CORE_DEFAULT


_IDEATE_POINTS_TAIL = """

THE TASK: come up with {k} IDEAS for tonight's posts — as the guy who wrote every caption above — a MIX of the two kinds:
- TRUTH: a pattern everyone recognizes but nobody posts, a delusion held with a straight face, or a coded take — stated in one plain sentence. A truth is something that KEEPS happening ("mfs always…", "broke dudes…", a standing fact about you) — never a one-off incident story. And it must be yours to see — if the internet already memed it, it's taken.
- BIT: constructed comedy you'd send to a buddy — a serious format hijacked with degenerate priorities, an unhinged comeback to a quote, an absurd cope in a "when…" frame, a backhanded encouragement. Describe the construction in one plain sentence (what's being hijacked/flipped and with what).
For each idea: its KIND and its STANCE — "you" (you're the bit) or "pointing" (calling out mfs/bro/men/everyone).

VARY THE AIM. Your catalog points a dozen different ways — at mfs, at broke dudes, at "dudes be like", at a girl who—, at everybody, at bro directly, at yourself, from inside a "when…" or a quote or a would-you-rather. A batch wears MANY of those; never let more than two ideas aim the same way with the same opener (a whole batch of "mfs will…" is one idea wearing ten hats).

VARY THE MOVE. A contradiction callout ("does X but also Y") is ONE move — not the whole set, and a batch full of it is the same joke eight times. Your catalog runs many: the delusion testimonial ("i've never…"), the exchange where you answer wrong on purpose ("she said… so i…"), the hijacked format / absurd math, the coded take the reader decodes, the "when…" cope with the villain externalized, the backhanded encouragement, the would-you-rather, the flex with a visible crack. Label each idea with its MOVE (short, your own words); spread the batch across at least four different moves, max two ideas per move.

No wordplay plans, no delivery notes — just what each caption IS. Everything in your catalog and under TAKEN TERRITORY is used; every idea must live on fresh ground.

Return ONLY JSON: {"points": [{"kind": "truth" | "bit", "move": "the move, in 2-4 words", "point": "one plain sentence", "stance": "you" | "pointing"}]}"""

_TYPE_IT_TAIL = """

THE TASK: below are {k} IDEAS — what each caption is supposed to be. For each of the strongest {n}, type it TWO different ways you might actually post it: said, not written; thrown away, not performed; under-explained so the reader finishes it. Two genuinely different takes — different wording, maybe different framing — so the better landing can win; the difference between a 4 and a 9 is usually the last five words. The catalog above is your own posted work (its premises are taken — it shows your sound); THE BAR is the standard to sit next to without embarrassing yourself. Keep each idea's kind and stance. Drop any idea you can't make land at that bar.

Return ONLY JSON, no prose: {"captions": [{"idea": <0-based idea index>, "takes": ["take one (\\n for line breaks)", "take two"]}]}"""


def _pick_takes(pairs: list[list[str]]) -> list[str]:
    """TAKE COMPETITION (round-5 alignment: the dominant miss was 'good premise, flat last five
    words'). Each idea arrives as up to two takes; one cheap call by the non-inverted judge
    (settings.chooser_model) keeps the take that lands better. Fail-safe: first take on any
    error. Selection-layer, not a craft rule — competition beats prescriptions here."""
    from app.config import settings
    if not pairs:
        return []
    real = [(i, p) for i, p in enumerate(pairs) if len(p) >= 2]
    picks = {i: 0 for i, _ in real}
    if real:
        listing = "\n\n".join(f"PAIR {j}:\n  [0] " + p[0].replace("\n", " / ")
                              + "\n  [1] " + p[1].replace("\n", " / ")
                              for j, (_, p) in enumerate(real))
        sys_p = (persona() + "\n\nYou typed two takes of each idea below. For each pair pick the "
                 "take you'd ACTUALLY post — the one that lands read cold; said, not written; the "
                 "last five words decide. A take whose referents drift (\"your mom\" then \"him\") "
                 "always loses; when takes are close, the one with the NAMED specific (the "
                 "Rothschilds, not \"every rich guy\") wins. "
                 "Return ONLY JSON: {\"picks\": [0 or 1 per pair, in order]}")
        try:
            out = complete_json(sys_p, listing, effort="low", max_tokens=800, tag="take-pick",
                                model=getattr(settings, "chooser_model", None) or None)
            s, e = out.find("{"), out.rfind("}")
            got = json.loads(out[s:e + 1]).get("picks", []) if s != -1 else []
            for j, (i, p) in enumerate(real):
                if j < len(got) and got[j] in (0, 1):
                    picks[i] = int(got[j])
        except Exception:  # noqa: BLE001 — competition must never break generation
            pass
    return [p[picks.get(i, 0)] if len(p) > 1 else p[0] for i, p in enumerate(pairs)]


def _select_best(caps: list[str], n: int) -> list[str]:
    """⚠️ SHELVED — MEASURED NEGATIVE (2026-07-09). Built as a best-of-more quality selector, but
    an LLM told to pick "the bangers you'd screenshot and send" pulls toward CORNY-QUOTABLE lines
    (definitional "X is just Y" aphorisms, worn takes) — the operator's named failure mode; it
    selected the worst captions in the pool. Same class as the reel-chooser inversion: an LLM's
    "quality" taste is not the operator's. NOT called; kept only for the record. The reliable
    quality signal is the operator's GRADES, never an LLM judge."""
    from app.config import settings
    pool = [c for c in caps if (c or "").strip()]
    if len(pool) <= n:
        return pool
    listing = "\n".join(f"[{i}] {c.replace(chr(10), ' / ')}" for i, c in enumerate(pool))
    sys_p = (persona() + "\n\n" + voice_core()
             + "\n\nYou drafted these captions tonight. Pick the " + str(n) + " you'd ACTUALLY "
             "post — the BANGERS, the ones that hit hardest read cold, the ones you'd screenshot "
             "and send. Every pick must clear the bar; leave the mid ones behind. Favor a spread of "
             "openers and angles so the set isn't one joke ten times — BUT never keep a weaker "
             "caption just to vary; quality first, variety only breaks near-ties.\n\n"
             'Return ONLY JSON: {"picks": [the ' + str(n) + " 0-based indices, best first]}")
    try:
        out = complete_json(sys_p, listing, effort="medium", max_tokens=600, tag="select-best",
                            model=getattr(settings, "chooser_model", None) or None)
        s, e = out.find("{"), out.rfind("}")
        idxs = json.loads(out[s:e + 1]).get("picks", []) if s != -1 else []
        seen, chosen = set(), []
        for i in idxs:
            if isinstance(i, int) and 0 <= i < len(pool) and i not in seen:
                seen.add(i)
                chosen.append(pool[i])
            if len(chosen) >= n:
                break
        if len(chosen) >= min(n, 2):
            return chosen[:n]
    except Exception:  # noqa: BLE001 — selection must never break generation
        pass
    # fallback: soft opener spread, then fill
    seen_op: dict[str, int] = {}
    kept, rest = [], []
    for c in pool:
        key = " ".join(c.lower().lstrip("🥷s'’ \"").split()[:2])
        seen_op[key] = seen_op.get(key, 0) + 1
        (kept if seen_op[key] <= 2 else rest).append(c)
    return (kept + rest)[:n]


_CONCRETE_TAIL = """

Write {n} NEW captions you'd post today — as this creator, in this exact voice.

The one rule above every other: CONCRETE, never abstract. Each caption is something the reader can SEE — a specific scene, a specific character doing a specific thing, a real recognized behavior, or you flexing the dumb thing with a straight face. Look at your catalog above: raccoons eating every night, a 50-year-old genuinely hyped explaining his 401k match, the hater losing it when the vending machine drops two bags of chips, a guy proud he's in debt from a car that hits 60 in 3 seconds. Those are the bar.

An ABSTRACT DEFINITION of a concept is DEAD — "an alarm clock is just your boss waking you up for free," "a job interview is just begging with better posture." The "X is just Y" reframe feels clever but it's a tweet about a concept, not you being a menace. Do not write a single one.

The specific detail that lands is EXACT and YOURS: the Rothschilds not "rich people," an LED sign with your name on it not "expensive stuff," "no homo back in 2019," a $997 course on how to sell a $997 course. Vague dies. Random-but-not-from-your-world dies. Land the last beat on a concrete image, never a soft summary word.

Brand new premises — everything in your catalog and in the taken list is used; go somewhere new but stay unmistakably you. As varied across your whole range as the catalog is.

Return ONLY JSON, no prose: {"captions": ["the caption (\\n for line breaks)", "..."]}"""


def _generate_v2(n: int, notes: str | None = None) -> list[dict]:
    """CONCRETE-FIRST, REFERENCE-DOMINATED generation (2026-07-09 rebuild, from the operator's
    caption-level analysis: his 10s are CONCRETE scenes/images/specific-in-world flexes; his 1s are
    ABSTRACT "X is just Y" definitions of concepts). One shot: the whole corpus is the voice (its
    concrete texture is the grounding), the persona embodies him, the voice core + north-star BAR
    name the concrete-not-abstract standard, and he writes fresh concrete captions. NO point-first
    ideation (it manufactured the abstract deaths), NO LLM judge in the pipeline (they prefer the
    abstract-clever — the select-best/chooser inversion), NO caps. Curation stays subtractive:
    morph/regurgitation drop → refine. The operator's GRADES are the only quality signal; they feed
    the corpus (richer concrete grounding) and the operator-editable voice core (var/voice_core.md)."""
    from app.caption import northstars
    refs = load_refs()
    if not refs:
        # a profile whose voice has no corpus must FAIL LOUDLY — generating from an empty wall
        # produces generic feed-slop and north-star noun-swaps (2026-07-09: a fresh profile
        # quietly shipped a whole batch this way and read as a system regression)
        raise RuntimeError("this profile's voice has no references — pick a voice (e.g. Base) "
                           "on the Generation Studio voice cards before generating")
    note = (notes or "").strip()
    ref_block = "\n\n".join((r.get("caption") or "").strip() for r in refs
                            if (r.get("caption") or "").strip())
    core = voice_core()
    ns_block = northstars.block()
    bar = (f"\n\nTHE BAR — captions the operator holds up as the standard (this concrete, this "
           f"specific; their premises are taken):\n{ns_block}" if ns_block else "")
    system = (persona() + _BRIDGE.format(references=ref_block) + core + bar
              + _CONCRETE_TAIL.replace("{n}", str(n)))
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + f"Recently generated — also used, go elsewhere: {_avoid_block()}\n\n"
        + f"Write {n} captions."
    )
    caps: list[str] = []
    for _attempt in (1, 2):   # a truncated/malformed JSON is retryable, not fatal
        text = complete_json(system, user, effort="high", max_tokens=4000,
                             cache_system=True, tag="batch-captions")
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            try:
                caps = [c.strip() for c in json.loads(text[s:e + 1]).get("captions", [])
                        if isinstance(c, str) and c.strip()]
            except json.JSONDecodeError:
                caps = []
        if caps:
            break
        print("[v2] caption parse failed — retrying", flush=True)
    out = [{"text": c, "anchor_ref": None, "anchor_refs": []} for c in caps[:n]]
    out = _coherence_gate(refine(_drop_ref_copies(out)))
    for c in out:
        c["caption_id"] = _cid(c.get("text") or "")
    log_generated([c.get("text", "") for c in out])
    return out


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Grade-weighted rotation-anchored generation (v1). Each candidate carries its `anchor_ref` so
    future grades attribute back exactly. Routed to _generate_v2 unless settings.generation_engine
    is pinned to "v1" (rollback knob)."""
    from app.config import settings
    if (getattr(settings, "generation_engine", "v2") or "v2") == "v2":
        return _generate_v2(n, notes)
    refs = load_refs()
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    anchors = _pick_anchors(refs, n)
    anchor_block = _render_anchors(anchors)   # craft-deepened when _CRAFT is on (A/B); plain otherwise
    avoid = _avoid_block()
    note = (notes or "").strip()
    craft_note = (" Each anchor also names THE CRAFT of its landing — the exact move that makes it hit; "
                  "land yours with that same craft (as exact and concrete — no fuzzy noun, no almost-right payoff)."
                  if _CRAFT.get() else "")
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Here are " + str(n) + " of your own sharp captions, each with WHY IT LANDS — these set your VOICE, "
        "your range, and the BAR." + craft_note + " Write " + str(n) + " NEW captions, one sparked by each (in order), and let them "
        "come NATURALLY. Hit that same bar, but DON'T force a shape: keep a format's structure ONLY when the "
        "structure IS the joke and it lands genuinely fresh — otherwise just write the sharpest thing in your "
        "voice and let the form follow the idea. A mechanical fill-in-the-blank of the template is dead — scrap it "
        "and write the one you'd post unprompted. THE EXCEPTION is a FRAME anchor — a \"POV:\", a \"How bro looks "
        "at me…\", a \"when…\", a Mom:/Officer: dialogue, a would-you-rather: there the frame IS the joke's "
        "species, so write yours as the same KIND of frame on a completely fresh moment — never converted into a "
        "written statement. A big share of your real captions are frames that live over the footage, and they hit "
        "hardest short. Keep your exact hyper-specificity; never generic, corporate, or "
        "poetic. Make the " + str(n) + " as VARIED from each other as your references are — in LENGTH too: a real "
        "share of your best references are dead-simple and under 12 words, so when an idea lands short, LEAVE it "
        "short:\n\n"
        + anchor_block
        + f"\n\n(Only the IDEA must be new — every format and opener you use stays fully in play. "
          f"Recently used ideas, don't re-tread them: {avoid})\n\n"
        + f"Return {n} captions — one per anchor, in order, each echoing its anchor's 0-based index "
        "(ANCHOR 1 → 0, ANCHOR 2 → 1, …). ONLY JSON, no prose: "
        '{"candidates": [{"anchor": <0-based anchor index>, "text": "the caption (\\n for line breaks)"}]}'
    )
    text = complete_json(voice_system(ref_block), user, effort="high", max_tokens=4000, tag="batch-captions")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        cands = json.loads(text[start:end + 1]).get("candidates", [])
    except json.JSONDecodeError:
        return []
    out = []
    # ECHO-based anchor attribution. Positional zip (anchors[i] <-> cands[i]) silently mis-attributed
    # every caption after a dropped/reordered candidate — corrupting grade attribution and, through it,
    # rotation weighting. Each candidate must echo its anchor's index; a missing/invalid/duplicate echo
    # DROPS that candidate (visible in the [echo] ledger) — never a positional guess.
    claimed: set[int] = set()
    dropped = 0
    for c in cands[:n]:
        if not (isinstance(c, dict) and (c.get("text") or "").strip()):
            continue
        ai = c.pop("anchor", None)
        if not isinstance(ai, int) or isinstance(ai, bool) or not 0 <= ai < len(anchors) or ai in claimed:
            dropped += 1
            continue
        claimed.add(ai)
        rid = anchors[ai].get("ref_id")
        c["anchor_ref"] = rid                       # back-compat (singular)
        c["anchor_refs"] = [rid] if rid else []     # provenance -> exact grade attribution
        out.append(c)
    if dropped:
        print(f"[echo] batch-captions dropped={dropped}/{len(cands)} candidates "
              "(missing/invalid/duplicate anchor echo)", flush=True)
    out = _coherence_gate(refine(_drop_ref_copies(out)))  # regurgitation drop -> subtractive edit -> coherence gate
    for c in out:
        c["caption_id"] = _cid(c.get("text") or "")     # hash the FINAL (post-refine) text
    log_generated([c.get("text", "") for c in out])
    return out


def generate_independent(k: int = 3, notes: str | None = None, audio_energy: str | None = None) -> list[dict]:
    """k INDEPENDENT single-caption generations for best-of-N selection (the reel chooser layer).

    Each candidate rides a DISTINCT anchor (one usage update, no race) and is generated in its OWN
    call — no shared batch, no avoid-list cross-suppression between the k — so each is the model's
    own best single shot. Runs the k calls in parallel. Returns candidate dicts {text, anchor_ref,
    caption_id} — the available captions, each tagged with the anchor it came from, so the reel can
    record which captions it chose between (production grading + the closed loop).
    """
    from app.config import settings
    if (getattr(settings, "generation_engine", "v2") or "v2") == "v2":
        return _generate_v2(max(1, k), notes)
    refs = load_refs()
    anchors = _pick_anchors(refs, max(1, k), produce=True)   # production slate: quality-weighted rotation
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    avoid = _avoid_block()
    note = (notes or "").strip()

    def one(anchor: dict) -> dict | None:
        user = (
            (f"Lean (soft): {note}\n\n" if note else "")
            + "Here's one of your sharp captions, with WHY IT LANDS — it sets your voice and your bar. Write a "
            "NEW caption that hits at that same bar, but let it come NATURALLY: keep its shape ONLY when that "
            "shape IS the joke and it lands genuinely fresh — otherwise just write the sharpest thing in your "
            "voice and let the form follow. A mechanical fill-in of the template is dead. EXCEPTION: if this "
            "anchor is a FRAME (a \"POV:\", a \"How bro looks…\", a \"when…\", a dialogue, a would-you-rather), "
            "the frame IS the joke's species — write the same KIND of frame on a completely fresh moment, never "
            "converted into a written statement; frames live over the footage and hit hardest short. Your best "
            "lines are often dead-simple and SHORT — when the idea lands in 10 words, that's the caption; never "
            "pad past the joke. Keep your exact specificity; never generic, corporate, or poetic:\n\n"
            + _anchor_render("ANCHOR", anchor) + "\n\n"
            f"(Only the IDEA must be new — every format and opener you use stays fully in play. "
            f"Recently used ideas, don't re-tread them: {avoid})\n\n"
            'Write ONE caption. ONLY JSON, no prose: {"text": "the caption (\\n for line breaks)"}'
        )
        # the k parallel candidates share ONE identical system (persona+refs+mechanics, several
        # thousand tokens) — cache it: first call writes, the rest read at ~10% of input price
        text = complete_json(voice_system(ref_block), user, effort="high", max_tokens=1500,
                             cache_system=True, tag="candidate")
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e == -1:
            return None
        try:
            t = (json.loads(text[s:e + 1]).get("text") or "").strip()
        except json.JSONDecodeError:
            return None
        return {"text": t, "anchor_ref": anchor.get("ref_id")} if t else None

    # copy_context() is evaluated HERE (request thread), carrying the active test backend into each
    # worker; a fresh snapshot per task avoids concurrent re-entry. No-op for production (backend None).
    # SEQUENTIAL-FIRST for the prompt cache (measured): a parallel fan-out races the cache — the k
    # simultaneous calls each pay the 1.25x WRITE and read nothing (and a primer's entry isn't
    # propagated in time either — 1/5 reads observed). Candidate 1 runs alone and pays the single
    # write; by its completion the entry is warm fleet-wide and candidates 2..k all READ at ~10%.
    raw = []
    if anchors:
        first = contextvars.copy_context().run(one, anchors[0])
        if first:
            raw.append(first)
        if len(anchors) > 1:
            with ThreadPoolExecutor(max_workers=max(1, k)) as ex:
                futs = [ex.submit(contextvars.copy_context().run, one, a) for a in anchors[1:]]
                raw += [c for c in (f.result() for f in futs) if c]
    out = [c for c in _coherence_gate(refine(_drop_ref_copies(raw))) if (c.get("text") or "").strip()]
    for c in out:
        c["caption_id"] = _cid(c.get("text") or "")
    log_generated([c.get("text", "") for c in out])
    return out
