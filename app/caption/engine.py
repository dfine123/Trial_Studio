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
- PRECISION. The twist maps EXACTLY — the two halves line up perfectly. Approximate or almost-funny is dead.
- DEADPAN CONFIDENCE. Stated flat, like it's obvious, even when it's unhinged.
- HYPER-SPECIFIC + VERY-ONLINE. Real specifics — named things, real numbers, real slang, emoji when it lands — never vague.
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
    """Compose the system prompt: per-profile PERSONA + the profile's references + the shared FORMAT base."""
    return persona() + _BRIDGE.format(references=ref_block) + _MECHANICS


def _pick_anchors(refs: list[dict], n: int) -> list[dict]:
    """n DISTINCT reference anchors. Rotates least-used-first for coverage, then weights by the
    GRADE signal: chronically-killed refs drop out, proven winners recur sooner. Distinct trait per
    batch for tonal spread, gambling soft-capped."""
    usage = _load_json(profiles.ref_usage_path())
    scores = _load_json(profiles.ref_scores_path())

    def _stat(r: dict) -> tuple[int, int, int]:
        s = scores.get(r.get("ref_id") or "", {})
        return s.get("keep", 0), s.get("kill", 0), s.get("best", 0)

    def is_failer(r: dict) -> bool:  # chronic LOW-KEEP-RATE format -> drop from rotation
        k, x, b = _stat(r)
        rate = (k + b) / (k + x) if (k + x) else 1.0
        return rate < 0.25 and x >= 4 and x > k + 3  # genuinely killed most of the time, with volume

    def is_winner(r: dict) -> bool:  # proven high-keep-rate format -> recur sooner
        k, x, b = _stat(r)
        rate = (k + b) / (k + x) if (k + x) else 0.0
        return (k + x) >= 6 and rate >= 0.6

    healthy = [r for r in refs if (r.get("caption") or "").strip() and not is_failer(r)]
    if len(healthy) < n:  # safety: too many dropped -> fall back to all non-empty
        healthy = [r for r in refs if (r.get("caption") or "").strip()]
    random.shuffle(healthy)  # random tiebreak among equally-used
    by_usage = sorted(healthy, key=lambda r: usage.get(_ref_key(r), 0))  # least-used first

    anchors: list[dict] = []
    seen_traits: set[str] = set()
    gambling = [0]

    def try_add(r: dict) -> None:
        if len(anchors) >= n or (r.get("persona_trait") or "?") in seen_traits:
            return
        if _is_gambling(r):
            if gambling[0] >= 2:
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
    anchor_block = "\n\n".join(_anchor_render(f"ANCHOR {i + 1}", a) for i, a in enumerate(anchors))
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Here are " + str(n) + " of your own real captions, each with WHY IT LANDS — the mechanism that "
        "makes it hit. For EACH anchor, write a NEW line in YOUR voice that lands the SAME WAY (same mechanism, "
        "same sharpness) on a genuinely FRESH subject. Transpose the WHY — do NOT re-skin the sentence or rewrite "
        "its joke. It has to sound unmistakably like YOU — the captions above ARE your voice and the bar — never "
        "cleaned up, corporate, or poetic. Match the anchor's exact hyper-specificity:\n\n"
        + anchor_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per anchor, in order. ONLY JSON, no prose: "
        '{"candidates": [{"text": "the caption (\\n for line breaks)"}]}'
    )
    text = complete_json(voice_system(ref_block), user, effort="high", max_tokens=4000)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        cands = json.loads(text[start:end + 1]).get("candidates", [])
    except json.JSONDecodeError:
        return []
    out = []
    for i, c in enumerate(cands[:n]):
        if isinstance(c, dict) and (c.get("text") or "").strip():
            rid = anchors[i].get("ref_id") if i < len(anchors) else None
            c["anchor_ref"] = rid                       # back-compat (singular)
            c["anchor_refs"] = [rid] if rid else []     # provenance -> exact grade attribution
            out.append(c)
    out = refine(out)  # subtractive edit; preserves provenance fields (dict(c)) + order/count
    for c in out:
        c["caption_id"] = _cid(c.get("text") or "")     # hash the FINAL (post-refine) text
    log_generated([c.get("text", "") for c in out])
    return out


def generate_independent(k: int = 3, notes: str | None = None, audio_energy: str | None = None) -> list[str]:
    """k INDEPENDENT single-caption generations for best-of-N selection (the reel chooser layer).

    Each candidate rides a DISTINCT anchor (one usage update, no race) and is generated in its OWN
    call — no shared batch, no avoid-list cross-suppression between the k — so each is the model's
    own best single shot. Runs the k calls in parallel. Returns refined candidate texts.
    """
    refs = load_refs()
    anchors = _pick_anchors(refs, max(1, k))
    random.shuffle(refs)
    ref_block = "\n\n".join(
        (r.get("caption") or "").strip() for r in refs if (r.get("caption") or "").strip()
    )
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()

    def one(anchor: dict) -> str | None:
        user = (
            (f"Lean (soft): {note}\n\n" if note else "")
            + "Here's one of your own real captions, with WHY IT LANDS — the mechanism that makes it hit. "
            "Write a NEW line in YOUR voice that lands the SAME WAY (same mechanism, same sharpness) on a "
            "genuinely fresh subject. Transpose the WHY; do NOT re-skin the sentence or rewrite its joke. Sound "
            "unmistakably like YOU — the captions above ARE your voice and the bar — never corporate or poetic:\n\n"
            + _anchor_render("ANCHOR", anchor) + "\n\n"
            f"(Don't rehash these exact recent lines: {avoid})\n\n"
            'Write ONE caption. ONLY JSON, no prose: {"text": "the caption (\\n for line breaks)"}'
        )
        text = complete_json(voice_system(ref_block), user, effort="high", max_tokens=1500)
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e == -1:
            return None
        try:
            return (json.loads(text[s:e + 1]).get("text") or "").strip() or None
        except json.JSONDecodeError:
            return None

    with ThreadPoolExecutor(max_workers=max(1, k)) as ex:
        raw = [c for c in ex.map(one, anchors) if c]
    cands = [c.get("text", "") for c in refine([{"text": c} for c in raw]) if (c.get("text") or "").strip()]
    log_generated(cands)
    return cands
