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
from concurrent.futures import ThreadPoolExecutor

from app import profiles
from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs


def _drop_ref_copies(cands: list[dict]) -> list[dict]:
    """Drop candidates that regurgitate a corpus REFERENCE near-verbatim (the anchor is a spark,
    never the output). An elite anchor sometimes comes back as itself, and the chooser rightly
    picks it — it IS a proven line — so the creator would end up re-posting their own reference
    (round-2 grading: 3 of 13 'winners' were corpus copies). Mechanical curation, not a prompt
    rule; if everything got dropped (pathological), keep the originals rather than return nothing."""
    from app.corpus.promote import _too_similar
    ref_texts = [(r.get("caption") or "") for r in load_refs() if (r.get("caption") or "").strip()]
    kept = [c for c in cands
            if not any(_too_similar(c.get("text") or "", t) for t in ref_texts)]
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


def _avoid_stub(c: str, stub_words: int = 9) -> str:
    """One line's premise stub: FORMAT MARKERS are stripped FIRST, then the first words of the
    CONTENT are taken — so the avoid list describes used IDEAS, never used openers. ⚠️ Regression
    this fixes: raw first-9-word stubs put the format marker itself in the list ("POV: …" ×30,
    "🥷s …") under a "don't rehash these openers" instruction — which suppressed entire VALIDATED
    format species (operator caught POV/🥷/sincere vanishing from production)."""
    t = (c or "").replace("\n", " / ").strip()
    while t and t[0] == "🥷":
        t = t.lstrip("🥷").lstrip("s'’ ").strip()
    low = t.lower()
    for m in _FRAME_MARKERS:
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


def _pick_anchors(refs: list[dict], n: int) -> list[dict]:
    """n DISTINCT reference anchors. Rotates least-used-first for coverage, then weights by the
    GRADE signal: chronically-killed refs drop out, proven winners recur sooner. Distinct trait per
    batch for tonal spread, gambling soft-capped."""
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
        k, x, b = _stat(r)
        rate = (k + b) / (k + x) if (k + x) else 0.0
        return (k + x) >= 6 and rate >= 0.6

    healthy = [r for r in refs if (r.get("caption") or "").strip()]
    random.shuffle(healthy)  # random tiebreak among equally-used
    # least-used first; failers carry a virtual-usage penalty so they cycle less often but always return
    by_usage = sorted(healthy, key=lambda r: usage.get(_ref_key(r), 0) + (3 if is_failer(r) else 0))

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

    # reserve ~2 slots for proven WINNERS (amplify), least-used winner first so they still rotate
    n_win = min(2, max(1, n // 4))
    for r in by_usage:
        if len(anchors) >= n_win:
            break
        if is_winner(r):
            try_add(r)
    # SPECIES FLOOR (operator rule: validated species must never just disappear from batches) —
    # every real batch carries at least one FRAME-format anchor (POV / 🥷 / would-you-rather /
    # wtf-is / when / how-bro — the frame-species exception then keeps it a frame in output) and
    # one SINCERE anchor (the largest seed cluster, structurally diluted by joke-heavy promotions:
    # 17 seeds vs 2/47 promoted — measured 2026-07-04).
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


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Grade-weighted rotation-anchored generation. Each candidate carries its `anchor_ref` so future
    grades attribute back exactly (closing the loop)."""
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
    refs = load_refs()
    anchors = _pick_anchors(refs, max(1, k))
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
