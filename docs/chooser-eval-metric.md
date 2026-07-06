# Chooser eval metric — what 0.226 actually measures, and what chance scores

*Task 3 of the phase-1 fix pass. READ-ONLY investigation (canon rule 3: no chooser or harness
changes). Measurements taken 2026-07-05 against production (`POST /api/chooser/eval`,
active voice: Austin/Base).*

## 1. The exact metric, as implemented

`POST /api/chooser/eval` in `app/main.py` (verbatim, abridged to the scoring logic):

```python
pairs = [g for g in grade_store.load_grades() if g.get("type") == "pairwise"]
recs = reels.graded()
cases, correct, picked_loser, picked_other = 0, 0, 0, 0
seen = set()
for g in pairs:
    w, l = norm(g.get("winner")), norm(g.get("loser"))
    if not w or not l or (w, l) in seen:
        continue
    seen.add((w, l))
    rec = next((r for r in recs
                if {w, l} <= {norm(c.get("text")) for c in (r.get("candidates") or [])}), None)
    if rec is None:
        continue
    cands = [c.get("text") or "" for c in rec.get("candidates") or []]
    pick = norm(choose_best(cands))
    cases += 1
    verdict = "correct" if pick == w else ("picked_loser" if pick == l else "picked_other")
    ...
return {... "accuracy": round(correct / cases, 3) if cases else None ...}
```

In words: each **deduplicated** `(winner, loser)` pairwise record is matched to the first graded
reel whose recorded candidate set contains **both** texts (whitespace/case-normalized). The
**current** chooser then re-picks from that reel's full candidate list. A case is `correct` only
if the pick text-equals the winner; `picked_loser` if it equals the operator-rejected posted
caption; `picked_other` otherwise. **accuracy = correct / cases.** Unmatched pairs are silently
skipped, so the denominator is "matched cases", not "stored records".

## 2. The ground-truth dataset

- **How a record is born:** the operator writes a grading note endorsing a specific unchosen
  candidate ("X would have been an 8"); the note miner (`taste.learn_from_reel`) text-matches the
  quote to that reel's real candidate list and stores
  `{"type": "pairwise", "winner": <endorsed>, "loser": <posted>}` in the voice's `grades.jsonl`.
- **Matched case count today: 22** (the harness's own `cases`). The raw stored-record count is
  not remotely readable (grades.jsonl lives on the volume and no endpoint exposes it; adding one
  was out of scope for a read-only task) — it is ≥ 22.
- **Candidate-set sizes:** all 22 matched cases resolve to reels with **exactly 5 candidates**
  (best-of-5 era). Size distribution `{5: 22}`, zero unmatched, zero ambiguous
  (reconstructed by matching each case's winner against `/api/reels/graded` candidate lists).
- **Adverse selection (built-in):** a pairwise record only exists when the operator *flagged a
  miss* — reels where the chooser already picked well produce no record. The eval set is
  therefore, by construction, dominated by past chooser failures. It measures "does the current
  chooser still fail where some past chooser failed", not general selection accuracy. It is also
  a **living set**: every graded round appends records, so the number is not comparable across
  time unless the set is frozen.

## 3. Reproducing the baseline — it did NOT reproduce

| run | cases | correct | picked_loser | picked_other | accuracy |
|---|---|---|---|---|---|
| recorded baseline (2026-07-02, commit ee21182) | 31 | 7 | — | — | **0.226** |
| this investigation (2026-07-05) | 22 | 1 | 15 | 6 | **0.045** |

Two structural reasons the number moved, both worth flagging:

1. **The chooser's effective prompt changed without an eval re-run.** The chooser system prompt
   is `_PICK_HEAD + persona.md + _PICK_TAIL` — the live persona is *injected into the chooser*.
   The persona was rewritten twice since the baseline was recorded (2026-07-04
   unemployed-not-poor rewrite; 2026-07-05 spectacle-spending line). Canon rule 3 gates chooser
   *code* changes on the harness, but persona edits slip through that gate while still changing
   chooser behavior. (No fix shipped here — read-only task — but this is the loophole.)
2. **The eval set itself is living.** Round-3 mining added new pairwise records (including
   multi-endorsement capture), and the matched-case set changed from 31 to 22 — the drop itself
   is unexplained from remote data (records only ever accumulate; matching is deterministic) and
   deserves a look when grades.jsonl is inspectable. Either way, 0.226-on-31-cases and
   0.045-on-22-cases are **different datasets** — the comparison is not apples-to-apples.

## 4. Empirical chance baseline

A seeded uniform-random picker (`random.Random(42)`) run through the same verdict logic over the
same 22 cases (each n=5, winner occupies one slot), **1,000 resamples**:

- **chance accuracy = 0.201 ± 0.086** (mean ± std across resamples)
- analytic cross-check: Σ(1/nᵢ)/N = 0.200 ✓
- chance `picked_loser` rate is likewise 0.200 (the loser also occupies one slot of 5)

## 5. Verdict (plain language)

**On this data, today's chooser is not just indistinguishable from chance — it is measurably
anti-correlated with the operator's taste.** Random picking would get about 4.4 of 22 cases
right (20%); the chooser got 1 (4.5%), which is on the edge of statistical significance below
chance (exact binomial: P(≤1 correct | chance) ≈ 0.048). The loud signal is `picked_loser`: the
chooser re-picked the operator-rejected caption in 15 of 22 cases (68%) where chance would hit
it in ~4.4 (20%) — the probability of that happening by luck is about **1 in 760,000**
(P ≈ 1.3×10⁻⁶). In plain terms: on the reels where the operator said "you should have posted X,
not Y", the current chooser doesn't just miss X — it actively re-chooses Y. The rejected lines
have a quality that systematically attracts the chooser ("chooser-bait" — clever-seeming lines
the model over-rates), and that pull is stronger than one-in-five luck. Two honest caveats:
n=22 is small, and the set over-represents historical failures by construction — but neither
caveat rescues a 68% loser-pick rate. Also note the recorded 0.226 baseline no longer
reproduces because both the injected persona and the living eval set have changed since it was
recorded; any future chooser work needs a frozen snapshot of this set and a re-baselined number
before anything ships against it.

*Measurement artifacts: `tmp/forensics/eval_probe.json` (harness output, sizes, chance
simulation), `tmp/eval_metric_probe.py` (the probe), `tmp/eval_binom.py` (exact binomials).
Nothing was shipped; no chooser, harness, or data files were modified.*
