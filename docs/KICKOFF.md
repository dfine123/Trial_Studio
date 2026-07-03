# Session kickoff prompt

Paste this as the first message of a new Claude Code session in this repo (fill in the task at the end).

---

**Trial Studio session kickoff.** Before doing anything else, ground yourself:

1. **Read `CLAUDE.md` in the repo root end-to-end.** It is the project brain — architecture, THE CANON
   (non-negotiable principles), ops runbook, per-profile state, and my standing working-style
   directions. Treat its principles as binding constraints. If anything in it contradicts live
   reality, trust live reality — and update CLAUDE.md as part of your work so context never rots.

2. **Ground in LIVE state** (the Railway app is the source of truth — not the repo, not old notes):
   - `git log --oneline -1` vs `GET https://trialstudio-production-8adf.up.railway.app/health` —
     confirm the deployed commit matches origin/main; flag drift.
   - Log into the app (`POST /api/login`, dfine/cool123) and pull: `/api/profiles` (which profile is
     ACTIVE — everything is scoped to it), `/api/refs/audit` (active corpus size + retired check),
     `/api/reels/pending` + `/api/reels/graded` counts, `/api/drive/status`.
   - Check `railway whoami` — if the CLI isn't logged in on this machine, tell me (one-time
     `railway login` enables env/deploy management from here).
   - Give me a compact state summary (deployed sha, profiles + corpus sizes, pending/graded reels,
     drive connections, any anomalies) BEFORE starting the task.

3. **Standing rules for every change** (details in CLAUDE.md — these are the ones people break):
   - Improve the CORE GENERATOR; don't overcomplicate. Grounding over transform layers — never add
     rules or negative examples to generation prompts.
   - Never eliminate anything because it scored low — a miss is evidence about an execution, not a
     verdict on a format.
   - Chooser/selection changes ship ONLY if `POST /api/chooser/eval` doesn't regress the baseline.
   - Measure corpus-vs-pool-vs-chosen before attributing a quality drift to a layer.
   - Verify everything LIVE (deploy → run → show real output/numbers) before calling it done.
   - Long server jobs (learn, re-embed) 502 at Railway's edge but keep running — poll counters and
     re-run idempotently. Deploys KILL in-flight background workers — never deploy mid-sync/repair.
   - After a grading round: pull `/api/reels/graded`, analyze themes with counts, map each theme to
     the right layer with evidence, run `/api/reels/learn` (loop until the corpus size is stable),
     verify with a fresh pool.

My task for this session: **[DESCRIBE THE TASK]**
