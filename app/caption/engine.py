"""Caption engine — reference-dominated, full-range, ROTATION-anchored + GRADE-WEIGHTED (closed loop).

Voice: a young terminally-online get-rich guy, raw money slang, allergic to corporate/poetic.
Coverage: each batch slot is anchored to a DISTINCT real reference, rotated least-used-first through
the whole corpus so every FORMAT gets covered and nothing repeats until cycled.

CLOSED LOOP (the "naturally avoid / naturally amplify" layer): every graded caption is attributed
back to the reference/format it came from (tmp/attribute_grades.sh -> var/ref_scores.json: per-ref
keep/kill/best). `_pick_anchors` then weights the rotation by that signal — chronically-killed
formats (e.g. crime-term wordplay the user keeps killing "gay") drop OUT of rotation, proven winners
recur more. The user's grading reshapes which formats the engine reaches for. No in-context kill-
steering (selection only), no hard-coded format bans, no judge. Falls back to pure rotation when
scores are absent.
"""
from __future__ import annotations

import json
import os
import random
from concurrent.futures import ThreadPoolExecutor

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
_REF_USAGE_PATH = os.path.join("var", "ref_usage.json")
_REF_SCORES_PATH = os.path.join("var", "ref_scores.json")


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
    os.makedirs("var", exist_ok=True)
    tmp = _REF_USAGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(usage, f)
    os.replace(tmp, _REF_USAGE_PATH)


_SYS = """You ARE this creator — a young, terminally-online guy whose entire brain is getting rich. You're somewhere between broke and made-it, always on the come-up, and you run everything through money, status, and the grind. You talk in lowercase internet slang (bro, ahh, fym, 🥷, "broke ahh", "lock in", "we eating"), and your humor is blunt, degenerate, very-online — crude bits, flexing, anti-simp, hustle delusion, and the occasional degenerate gambling confession (ONE flavor, not your whole personality).

The one voice you physically cannot stand is fake-professional or soft. A LinkedIn post, a finance-bro pitch, a corporate email ("independent liquidity reallocation specialist", "let me run it by accounting", "diversify your side-hustle portfolio"), a motivational poster or fortune-cookie proverb ("the dog that dreams of hunting wolves", "no one remembers the man who folded") — that's the exact opposite of you, it makes your skin crawl. When you talk money it's bags, rent, the come-up, Cash App, daddy's money — street and real, never cleaned-up corporate-speak.

Below are your REAL captions — this is the voice, the range, AND the bar:

{references}

What every one of these shares (your instincts — feel them, don't check them off):
- THE TWIST. The setup primes one thing; the line flips to another — the GAP is the joke. A homophone decode ("Iran this, Iran that" -> "I ran up a bag"), a reframe ("we ain't broke, we pre-rich"), a bait-and-switch ("I bet you have hoes / ahh so close, I have a gambling problem"), a self-own ("you're broke because you don't work, I'm broke because I make bad financial decisions — we are not the same").
- PRECISION. The twist maps EXACTLY. "A fat chick saying she has big boobs is like an unemployed dude saying he has a day off" lands because the two map perfectly. Approximate or almost-funny is dead.
- DEADPAN CONFIDENCE. Stated flat, like it's obvious, even when it's unhinged ("be more like a crackhead").
- HYPER-SPECIFIC + VERY-ONLINE. Real specifics (vbucks, Adin Ross, 1099 vs W-2, a $200 casino trip), real slang, emoji when it lands.
- ALWAYS SHARP — never generic, never corporate, never a poster. Even your sincere lines are SPECIFIC truths or parody ("nobody is good at the start, nobody is bad after 1000 attempts")."""


def _pick_anchors(refs: list[dict], n: int) -> list[dict]:
    """n DISTINCT reference anchors. Rotates least-used-first for coverage, then weights by the
    GRADE signal: chronically-killed refs drop out, proven winners recur sooner. Distinct trait per
    batch for tonal spread, gambling soft-capped."""
    usage = _load_json(_REF_USAGE_PATH)
    scores = _load_json(_REF_SCORES_PATH)

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
    anchor_block = "\n\n".join(
        f"ANCHOR {i + 1}: {(a.get('caption') or '').strip()}" for i, a in enumerate(anchors)
    )
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(50)) or "(none yet)"
    note = (notes or "").strip()
    user = (
        (f"Lean (soft): {note}\n\n" if note else "")
        + "Here are " + str(n) + " of your own real captions — each one a DIFFERENT format you use. "
        "For EACH anchor, say something NEW in YOUR voice using that same format: the same structure, "
        "rhythm, and twist — but a fresh subject (never a rewrite of its joke). It has to sound "
        "unmistakably like YOU — lowercase, slangy, blunt, money-brained, very-online — never cleaned "
        "up, corporate, or poetic. Match the anchor's exact sharpness and hyper-specificity:\n\n"
        + anchor_block
        + f"\n\n(Don't rehash these exact recent lines: {avoid})\n\n"
        + f"Return {n} captions — one per anchor, in order. ONLY JSON, no prose: "
        '{"candidates": [{"text": "the caption (\\n for line breaks)"}]}'
    )
    text = complete_json(_SYS.format(references=ref_block), user, effort="high", max_tokens=4000)
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
            c["anchor_ref"] = anchors[i].get("ref_id") if i < len(anchors) else None
            out.append(c)
    out = refine(out)  # preserves anchor_ref (dict(c)) + order/count
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
            + "Here's one of your own real captions — a format you use. Say something NEW in YOUR "
            "voice using that exact format: same structure, rhythm, and twist — but a fresh subject "
            "(never a rewrite of its joke). Sound unmistakably like YOU — lowercase, slangy, blunt, "
            "money-brained, very-online — never corporate or poetic. Match its sharpness and "
            "hyper-specificity:\n\n"
            f"ANCHOR: {(anchor.get('caption') or '').strip()}\n\n"
            f"(Don't rehash these exact recent lines: {avoid})\n\n"
            'Write ONE caption. ONLY JSON, no prose: {"text": "the caption (\\n for line breaks)"}'
        )
        text = complete_json(_SYS.format(references=ref_block), user, effort="high", max_tokens=1500)
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
