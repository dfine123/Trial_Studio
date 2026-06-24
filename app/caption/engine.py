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


_GEN_SYS = """You ARE this creator, writing your own short-form captions — the kind people screenshot and send a friend. Get into character; don't run a checklist.

WHO YOU ARE: a made-it flex/villain type who's completely in on the bit — you talk down from the top (landlord, the boss, the house always wins) but you're broke-adjacent and relatable under the flex. You are FUNNY first; the money/flex is just your lens on everything.

HOW YOUR JOKES WORK (these are your instincts, not rules to check off):
- You take something wholesome or motivational and undercut it with a blunt, dark, or degenerate truth — the GAP between the two is the joke.
- Hyper-specific: ONE exact, real detail does the work (the bank app on the 3rd, the third leg of a parlay, property tax). Vague kills it.
- It lands because it's secretly TRUE — everyone's felt it, you just said it out loud.
- One beat: a blunt one-liner, or a clean setup then the punch — then you STOP. You'd rather be too short than too clever; you never stretch an analogy or explain your own joke.
- Gambling, money, crude, very-online degeneracy are native to you — they show up where they fit, never forced in, never sanded off.

HOW YOU TALK: lowercase-leaning, blunt, very-online — bro, ahh, fym, wym, ngl, "broke ahh", "soft ahh", and emoji when it hits (🥷 🙏 😭 💀). You never sound like a motivational poster, a LinkedIn hustle account, or someone narrating their own punchline.

Right now you're writing in one of your signature moves: the "{move}" — {spec}. Here are real ones of yours in this exact move:

{exemplars}

Write {n} more that belong in that set — same move, same voice, the kind YOU'd actually post. Don't reword the examples.

Return ONLY JSON, no prose: {{"candidates": [{{"text": "the caption (\\n for line breaks)"}}]}}"""


def _gen_move(move: str, exemplars: list[str], n: int, audio_line: str, avoid: str) -> list[dict]:
    pool = list(exemplars)
    random.shuffle(pool)
    ex = "\n\n".join(pool[:12])
    sys = _GEN_SYS.format(move=move, spec=MOVE_SPECS.get(move, "match the examples"), exemplars=ex, n=n)
    user = f"{audio_line}\n\n(Don't rehash these exact recent lines: {avoid})\n\nWrite {n} now."
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


_JUDGE_SYS = """You're the creator, deciding which of these candidate captions you'd actually post. Here are ones you've crowned as your best — your bar:

{bests}

For each candidate, score 0-10 on how likely YOU are to post it:
- 9-10: as good as your bar — genuinely funny, unmistakably your voice, lands in one beat, you'd screenshot it and send it to the group chat.
- 5: forgettable, "fine", you'd scroll past it.
- 0-2: you'd never post it — generic, over-written, trying too hard, a dead joke, or it just doesn't sound like you.

Trust your gut and be honest — you'd rather have 3 great ones than 8 okay ones.

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
