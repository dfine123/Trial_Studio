# Session summary — 2026-07-12 · Telegram reference-recreation bot (static + dynamic)

Pick-up-where-we-left-off context for the reference-recreation feature built and dialed in
this session. Deeper standing context lives in [CLAUDE.md](../CLAUDE.md) (the portable brain);
this file is the session-level narrative.

## What exists now (all live on prod, commit `d83f1ba`)

**The bot: @treelz_copy_bot ("Treelz CopyCat").** The operator sends an Instagram reel link in
Telegram → the system downloads it (yt-dlp), extracts its AUDIO (the recreation reuses the same
track), reads the burned-in caption off sampled frames (Claude vision), and recreates the reel
for every profile toggled **"Reference active"** in the studio's left rail — each from that
profile's own clips — then uploads each result to Drive under
`treelz exports/<profile>/references/`. Progress streams back per-profile in the chat.
Credentials (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`) live in the **prod** Railway
service env only — never in this public repo. The bot answers ONLY the allowed user id;
everyone else is silently ignored (ignored senders are logged with their id).

**Static recreations (one caption throughout).** Caption copied 1:1 (personalization is
rare-by-design, fail-open verbatim) + **coherent clip selection** (operator rule: "consistent
across the same car(s) and generally clip setting"): the matcher ranks ONE subject/setting
family first, the selector skips the variety de-dup machinery and gives similarity-to-playing
clips an 8.0 cost bonus, and reuses a family clip over importing an off-family one. 5–9s reel.

**Dynamic recreations (caption changes partway — setup → payoff).** Detection is automatic
from the caption timeline (1 span = static path, 2+ = dynamic). The recreation follows the
REFERENCE's own clock: reel runs the reference's length (cap 40s), caption parts switch at the
reference's times, a cut is FORCED at every switch, and the switch snaps to the nearest audio
beat (same audio → same musical hit). Timing precision is two-pass: coarse 0.5s frame scan,
then a dense 0.1s vision pass pins each transition to ~±0.05s (ground-truth verified on the
kevoskoins reference: refined 4.95s vs true ~4.90s). Clips re-match **per part with role
awareness**: a low/"1HP" setup part wants mundane/unglamorous footage — luxury flexes are
explicitly a BAD fit there (the contrast IS the joke) — with a closest-in-sense fallback when
the library has no true "before" clips. Clips never repeat across parts.

## Verified end-to-end (operator's own references)

- Static: car reel ("depreciating asset" caption) → 2/2 recreations in Drive, coherent scenes.
- Dynamic: kevoskoins "even when your at 1HP… / you can still do 200 damage." → 2/2 in Drive;
  the setup slots picked the most mundane clips available in flex-heavy libraries
  (balcony-sunset + outdoor-meal for Check; the two "calm routine drive" clips for Austin),
  payoffs got the supercars/jets.

## Operating notes

- **Test path without Telegram:** `POST /api/debug/reference-intake {"url": ...}` (operator
  cookie). ⚠️ The Railway edge kills long responses — the `[ref]` stage prints in prod logs are
  the record (timeline spans, per-part clip picks, per-profile ✅/❌).
- **Railway CLI gotcha:** from this directory the CLI links to the DEMO service — always pass
  `--service Trial_Studio` for prod vars/logs/redeploys (a var-set once silently landed on demo
  and the bot didn't start).
- **Never probe Telegram `getUpdates` externally** — a second consumer 409-conflicts the live
  poller (it self-heals with a 10s backoff, but probes steal poll cycles). Verify via logs:
  `[tg] reference bot polling`.
- `sendMessage` 400 "chat not found" = that user hasn't pressed Start; bots can't initiate.
- An IG reel URL can be reconstructed from a downloaded filename's numeric media id: base64 the
  id with alphabet `A–Z a–z 0–9 - _` → the `/reel/<shortcode>/` (used to re-run the kevoskoins
  reference: id 3893513940737178108 → DYIikzlq7n8).
- Deploys kill in-flight recreations — don't push mid-run.

## Where the code lives

- `app/reference/intake.py` — download / audio / caption-timeline extraction (+ dense boundary
  refinement) / personalization / per-profile orchestration.
- `app/reference/telegram.py` — the long-poll bot (operator-only, daemon thread, lifespan-started
  only when both env vars are set; never in demo mode).
- `app/generate/generator.py` — `generate_reel(coherent_clips=)` (static) +
  `generate_dynamic_reel` (spans) + the three matcher prompts (standard / coherent / part-role).
- `app/generate/sequencer.py` — `select_segments(coherent=)` + `split_slots_at`.
- `app/render/compositor.py` — `compose_template_reel` overlays caption PNGs by time window.
- `app/drive/export.py` — `upload_reference` (per-profile `references/` subfolder).
- Tests: `tests/test_phase1.py` (41/41) — timeline grouping, boundary refinement precision,
  slot splitting, coherent-vs-default selection, personalization fail-open.

## Open threads / next steps

- Operator review of the latest dynamic pair (timing + setup/payoff contrast) — further dial-in
  knobs: the 8.0 coherence bonus, the 0.2s beat-snap window, span-matcher role prompt.
- Personalization beyond 1:1 stays deliberately rare; no changes planned until the operator asks.
- Recreations don't enter genlog/grading (by design — they're recreations, not generations).
- Multiple test pairs of the same two references currently sit in the Drive `references/`
  folders (from the dial-in runs) — the newest pair per reference is the current build's.
