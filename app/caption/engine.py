"""Move-based caption studio — DECOMPOSE -> COMPOSE -> CURATE.

The old monolith asked one prompt for N varied captions; it collapsed to the easy vein (gambling),
dropped most moves, and shipped whatever it spat. This rebuilds it around the creator's actual
MOVES (the joke-engines, clustered from the references + graded bests in `corpus/move_library.json`):

  1. DECOMPOSE — each MOVE has its own focused, reference-dominated generator that sees ONLY its
     own exemplars and writes only that move (timeline writes timelines, 🥷 writes 🥷s).
  2. COMPOSE — a director picks a rotating, audio-aware lineup of moves per batch, so the whole
     repertoire cycles and any one topic (gambling) self-limits to where it naturally fits.
  3. CURATE — a judge scores every candidate against the creator's crowned BESTS (the quality bar)
     and kills the nonsensical / generic / off-voice ones before they ship.

`generate()` keeps its old signature so the grading UI and reel pipeline are unchanged.
"""
from __future__ import annotations

import json
import os
import random
from concurrent.futures import ThreadPoolExecutor

from app.caption.llm import complete_json
from app.caption.refine import refine
from app.corpus import grades as _grades
from app.corpus.genlog import log_generated, recent_generated
from app.corpus.store import load_refs

_MOVE_LIB_PATH = "corpus/move_library.json"

# One-line spec per move (the mechanism) — supplements the exemplars for the generator + director.
MOVE_SPECS = {
    "sincere_reframe": "a sincere, sharp reframe or hard-won truth that lands clean in one beat",
    "anticope_callout": "calls out the REAL reason you're stuck — 'you're not X, you're Y'",
    "ninja_observational": "🥷's hate / are so broke / believe X — deadpan take on haters/everymen via the ninja emoji",
    "two_speaker_reveal": "someone says X, then a reveal/comeback (Mom/therapist/girl/cop: ... / me: ...)",
    "would_you_rather": "would you rather A or B — catch is a REAL tempting dilemma or an absurd vivid condition (never a dead riddle)",
    "analogy_x_is_like_y": "doing X is like doing Y — a savage, true-ringing analogy",
    "money_one_liner": "a blunt money/flex one-liner or reframe",
    "backhanded_encouragement": "fake-encouraging — 'keep [verb]ing bro, the world needs more [losers]'",
    "crackhead_aphorism": "'crackheads never say ...' / be more like [degenerate] — perverse motivation",
    "proverb_subversion": "opens like a proverb / wise saying, then subverts it",
    "timeline_comparison": "successful people's timeline (Zuckerberg at 19...) then 'and you're still X' / 'it's over bro'",
    "crude_hottake": "an absurd, confident, crude hot-take stated as fact",
    "anti_simp": "dunks on simping / being whipped",
    "self_own_extreme": "a relatable self-own pushed to an absurd extreme",
    "fake_stat": "fake escalating stats or absurd business math, played straight, into a gut-punch",
    "wym_callout": "'wym [X]?' — incredulous, dismissive callout",
    "flex_villain": "petty / villain flex — landlord / boss / made-it POV looking down",
}

# Which moves lean which audio energy (soft weighting only).
_REFLECTIVE_MOVES = {"sincere_reframe", "anticope_callout", "proverb_subversion",
                     "analogy_x_is_like_y", "timeline_comparison", "backhanded_encouragement"}
_HYPE_MOVES = {"flex_villain", "crude_hottake", "self_own_extreme", "ninja_observational",
               "two_speaker_reveal", "wym_callout", "anti_simp", "money_one_liner"}


def _load_moves() -> dict[str, list[str]]:
    try:
        lib = json.load(open(_MOVE_LIB_PATH))
        return {m: [c for c in cs if isinstance(c, str) and c.strip()]
                for m, cs in lib.items() if m != "unlabeled" and cs}
    except Exception:  # noqa: BLE001 — fall back to one pseudo-move of all references
        refs = [(r.get("caption") or "").strip() for r in load_refs() if (r.get("caption") or "").strip()]
        return {"voice": refs} if refs else {}


def _bests(k: int = 18) -> list[str]:
    try:
        b = [x for x in _grades.best_captions() if isinstance(x, str) and x.strip()]
        random.shuffle(b)
        return b[:k]
    except Exception:  # noqa: BLE001
        return []


def _plan_lineup(moves: dict[str, list[str]], n: int, audio_energy: str | None) -> dict[str, int]:
    """Director: pick a rotating, audio-aware lineup of moves and allocate counts (overgenerated
    ~1.6x so the judge has room to cut). Balance is structural — gambling has no special slot."""
    avail = [m for m in moves if moves[m]]
    if not avail:
        return {}

    def weight(m: str) -> float:
        s = 1.0
        if audio_energy == "low" and m in _REFLECTIVE_MOVES:
            s += 0.8
        if audio_energy in ("high", "rising", "mid") and m in _HYPE_MOVES:
            s += 0.6
        return max(0.1, s + random.random() * 0.9)  # jitter → different lineup each batch

    picked = sorted(avail, key=weight, reverse=True)[: min(8, len(avail))]
    target = int(n * 2.4) + 1  # overgenerate generously so the judge has a deep pool to rank
    alloc: dict[str, int] = {}
    i = 0
    while sum(alloc.values()) < target and i < target * 3:
        m = picked[i % len(picked)]
        alloc[m] = alloc.get(m, 0) + 1
        i += 1
    return alloc


_GEN_SYS = """You write ONE specific MOVE of a creator's short-form captions. Below are REAL examples of this exact move — match the MECHANISM, the voice, the slang, the formatting.

THE MOVE — "{move}": {spec}

REAL EXAMPLES (this IS the move + the voice — study the joke-engine):
{exemplars}

Write {n} NEW captions that run THIS move. Hard rules:
- Run the SAME joke-engine as the examples — not a different move.
- The creator's voice: very-online, blunt, hyper-specific, crude/degenerate is welcome where it fits, genuinely FUNNY.
- The punchline must actually LAND and make logical sense — no forced twist, no nonsense, no "almost a joke". If it doesn't make you exhale, it's wrong.
- PUNCHY: match the LENGTH and economy of the examples — land it in ONE clean beat and STOP. Do NOT over-explain, stretch the analogy, or tack on a second clause to seem clever (no "...like a salmon swimming upstream to die"). The shortest version that lands is the best version; if a clause can be cut, cut it.
- Don't force gambling/casino — only if it genuinely fits this line.
- Fresh — never copy or reword an example.

Return ONLY JSON, no prose: {{"candidates": [{{"text": "the caption (\\n for line breaks)"}}]}}"""


def _gen_move(move: str, exemplars: list[str], n: int, audio_line: str, avoid: str) -> list[dict]:
    pool = list(exemplars)
    random.shuffle(pool)
    ex = "\n\n".join(pool[:12])
    sys = _GEN_SYS.format(move=move, spec=MOVE_SPECS.get(move, "match the examples"), exemplars=ex, n=n)
    user = (f"{audio_line}\n\nDon't rehash these recently-shown lines:\n{avoid}\n\n"
            f"Write {n} fresh, genuinely funny '{move}' captions that clear the examples' bar. "
            f"Vary what they're ABOUT across the set — money, work, dating, family, status, everyday "
            f"absurd — and keep gambling/betting to a flavor, NOT the subject of most of them.")
    text = complete_json(sys, user, effort="high", max_tokens=1600)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        cands = json.loads(text[start:end + 1]).get("candidates", [])
    except json.JSONDecodeError:
        return []
    out = []
    for c in cands[:n]:
        if isinstance(c, dict) and (c.get("text") or "").strip():
            c["move"] = move
            out.append(c)
    return out


_JUDGE_SYS = """You rank ONE creator's candidate captions by quality. Here is their BAR — captions THEY crowned as their best. These are GREAT; they'd score 9-10:

{bests}

Score each candidate 0-10 against that bar:
- 9-10 = as good as the bar: blunt, PUNCHY, lands in one clean beat, genuinely funny, you'd screenshot it.
- 6-8 = solid and in-voice, but not elite.
- 3-5 = mediocre / forgettable / slightly off.
- 0-2 = broken: nonsense, no real joke, over-written or run-on, stretched analogy, buried punchline, off-voice, or generic.

Reward PUNCH — the bar is short and hits instantly. A short blunt line that lands (e.g. "shut up poor person", "i don't have a spending problem / i have a not-enough-money-to-spend problem") beats a long clever one every time. Penalize over-writing and stretched analogies HARD.

Return ONLY JSON: {{"verdicts": [{{"i": <index>, "score": <0-10>}}]}}"""


def _judge(cands: list[dict], bests: list[str]) -> list[dict]:
    if not cands:
        return []
    listing = "\n".join(f"[{i}] {(c.get('text') or '').replace(chr(10), ' / ')}" for i, c in enumerate(cands))
    scores: dict[int, float] = {}
    if bests:
        sys = _JUDGE_SYS.format(bests="\n".join(f"- {b.replace(chr(10), ' / ')}" for b in bests))
        for _ in range(2):  # retry once on a flaky non-JSON return
            try:
                text = complete_json(sys, "Rank these candidates:\n" + listing, effort="high", max_tokens=2000)
                start, end = text.find("{"), text.rfind("}")
                verdicts = json.loads(text[start:end + 1]).get("verdicts", []) if start != -1 else []
            except Exception:  # noqa: BLE001
                verdicts = []
            for v in verdicts:
                i = v.get("i")
                if isinstance(i, int) and 0 <= i < len(cands):
                    scores[i] = v.get("score", 5)
            if scores:
                break
    out = []
    for i, c in enumerate(cands):
        c = dict(c)
        c["score"] = scores.get(i, 5)  # neutral default so a candidate is never lost / un-scored
        out.append(c)
    out.sort(key=lambda c: c.get("score", 0), reverse=True)
    return out


def generate(
    audio_vibe: list[str] | None = None,
    audio_purpose: list[str] | None = None,
    audio_energy: str | None = None,
    notes: str | None = None,
    n: int = 8,
    clip_context: str | None = None,
) -> list[dict]:
    """Compose a batch from focused per-MOVE generators, then curate against the bests."""
    moves = _load_moves()
    if not moves:
        return []
    bests = _bests()
    avoid = "\n".join("- " + c.replace("\n", " / ") for c in recent_generated(40)) or "(none yet)"
    audio_line = f"Audio energy: {audio_energy or 'n/a'}. Notes: {(notes or '').strip() or 'none'}."

    alloc = _plan_lineup(moves, n, audio_energy)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_gen_move, m, moves[m], cnt, audio_line, avoid) for m, cnt in alloc.items()]
        pool: list[dict] = []
        for f in futures:
            try:
                pool += f.result() or []
            except Exception:  # noqa: BLE001 — one move failing shouldn't kill the batch
                pass

    judged = _judge(pool, bests) or pool
    # take the top n, but cap any single move to 2 so the batch spans the repertoire
    out, per_move = [], {}
    for c in judged:
        if len(out) >= n:
            break
        m = c.get("move", "")
        if per_move.get(m, 0) < 2:
            out.append(c)
            per_move[m] = per_move.get(m, 0) + 1
    for c in judged:  # if the cap left us short, fill from the rest
        if len(out) >= n:
            break
        if c not in out:
            out.append(c)
    out = refine(out)
    log_generated([c.get("text", "") for c in out])
    return out
