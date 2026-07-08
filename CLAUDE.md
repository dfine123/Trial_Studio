# Trial Studio — Project Context

Per-creator short-form (9:16) reel + caption generator. Clips flow IN from a creator's Google Drive,
get indexed (TwelveLabs + OpenCV), reels generate (caption-first → clip match → beat-cut → composite),
the operator grades finished reels, the system LEARNS from those grades, and validated reels flow OUT
to the operator's Drive. Deployed on Railway (`dfine123/Trial_Studio`, PUBLIC repo — never commit
secrets); service `Trial_Studio` in project `dynamic-emotion`, app URL
`trialstudio-production-8adf.up.railway.app` (login dfine/cool123). Local dev runs in WSL Ubuntu
(`.venv/bin/python`); Postgres + a persistent volume (`var/`) live on Railway.

## THE CANON — principles learned the hard way (do not relearn these)

1. **Grounding lifts; transform layers neuter.** There are two ways to change caption quality:
   GROUNDING (what the model sees before writing: references, persona, why_it_works) and TRANSFORMS
   (rules, post-processing passes, taste filters). Every grounding move in this repo's history raised
   quality; every transform/rule (a STANCE rule, a distilled-taste chooser filter, a craft-deepening
   pass — all reverted) narrowed the voice or measurably regressed. A/B'd and proven. Generation stays
   reference-DOMINATED + FULL-RANGE + embodied; preferences live in CURATION (chooser) or STRUCTURAL
   WIRING (rotation, caps, windows), never as generation-prompt rules or negative examples.
2. **A miss is evidence about an EXECUTION, not a verdict on the FORMAT.** Never eliminate/cull
   anything for scoring low — understand why, de-weight at most (rotation virtual-usage penalty),
   never drop. Grading must expand understanding, never shrink range.
3. **Chooser changes ship ONLY through the eval harness.** `POST /api/chooser/eval` replays the
   operator's own "should have picked X" corrections against the current chooser. Baseline history:
   0.194 → 0.226 (best-first modular). A change that regresses gets reverted no matter how reasonable
   it seemed (this caught a real one). ⚠️ 2026-07-05 investigation (docs/chooser-eval-metric.md):
   the 0.226 baseline NO LONGER REPRODUCES — current run scored 0.045 on 22 matched cases (all
   best-of-5; empirical chance 0.201±0.086) with picked_loser 15/22 (p≈1.3e-6): the chooser actively
   re-picks operator-rejected lines on this set. Two structural causes: the PERSONA is injected into
   the chooser, so persona edits change chooser behavior WITHOUT tripping this gate (loophole — two
   persona rewrites shipped since the baseline), and the eval set is LIVING (grows with every mined
   round; 31→22 matched cases, drop unexplained). **RESOLVED 2026-07-06 (commit d904510): the
   inversion was the JUDGE MODEL, not the prompt** — five prompt variants (incl. few-shot
   corrections: 9/11 tune by memorization, 0/11 holdout) failed to move opus-as-judge; sonnet-4-6 /
   sonnet-5 / haiku on the IDENTICAL prompt all drop loser-picks 17→2 with 6/22 correct. Shipped
   `settings.chooser_model="claude-sonnet-4-6"`; live harness 0.045→0.273, picked_loser 15→2.
   Standing benchmark for future chooser changes: the FROZEN set (tmp/forensics/eval_frozen.json,
   22 cases, seeded 11/11 tune/holdout — sonnet holdout baseline 3/11 correct, 1 loser). The
   generation-side judges (batch grading, lab, labeler, codex) still run the caption model — only
   SELECTION swaps judges.
4. **Measure corpus-vs-pool-vs-chosen before assigning a drift to a layer.** A "chooser problem" was
   generation-side twice (frame loss, length). Generate a raw pool and compare distributions first.
5. **Turn voice elements up/down via POSITIVE priming only** (persona slang list, reference mix,
   structural caps) — never "don't do X". Precedents: 🥷 emoji (removed from persona slang list, stays
   in refs), gambling (10% of refs is honest; anchor cap scales with batch size).
6. **The corpus IS the generator's brain, and grades feed it automatically.** `/api/reels/learn`
   mines notes (pairwise + off_voice) AND auto-promotes every operator-validated line (posted reels
   rated ≥8 + note-endorsed "would have been an 8/9" alts) into the profile's references with decoded
   why_it_works. Grade → learn → better generator. That's the whole loop. Round-3 upgrades (2026-07-05):
   the miner captures EVERY endorsed candidate per note (was singular — 3 round-3 notes each endorsed
   2 lines; half were silently lost), per-line claimed ratings, and operator-AUTHORED complete captions
   (verbatim-span + fuzzy-vs-candidates + standalone-post guards → `authored` grade records →
   `source=operator_authored` refs; a payoff FRAGMENT like "an LED sign with my name on it" must fail
   the standalone test — one misfiled as p066 and was pruned via `/api/debug/authored-prune`). The
   why_it_works labeler now receives the operator's own note (their punch-ups outrank the LLM's read).
   **DECODE SPLIT (2026-07-06, permanent architecture):** `why_it_works` = short (≤50w), ANCHOR-facing
   (rendered as WHY IT LANDS inside the voice); `why_full` = rich analysis, CONSOLIDATION-facing (codex
   evidence prefers it, seeds fall back); `generativity` = generative|singular (additive metadata for a
   later anchor-duty phase — consumed by NOTHING yet); `decode_v: 2` marks split refs (regen idempotency).
   272 promoted decodes across 3 voices regenerated by COMPRESSING why_full (mean 86→41w; seeds
   byte-identical; scripts/regen_promoted_decodes.py + /api/debug/regen-decodes, report volume-side).
   Batch generation now attributes anchors by ECHOED index (a dropped/reordered candidate no longer
   mis-attributes everything after it; invalid echoes DROP with an [echo] log — never positional). The
   lab codex is validated on rebuild (6 sections + complete ending; retry ×3 then keep-previous);
   `POST /api/debug/relabel-refs {ref_ids}` re-decodes existing refs with their source note folded in
   (used on p052–p065; also fixed p062's silently-null decode — a bare-except swallowed the label
   failure on a 9-rated ref). Austin's live persona gained the round-3 world texture via the
   adversary-approved CLASS-level line ("you spend on spectacle: things with your name on them, things
   that need an audience…" — concrete props like the LED sign deliberately NOT named: persona text
   bypasses rotation AND the regurgitation guard, the 🥷-list precedent) + "credit-score comeback
   arcs" added to the not-his-universe list. Post-ship distribution check: purchase-flavored 1/8.

## Voice architecture (two layers; voices are TOGGLEABLE per profile)

- **Shared FORMAT base** (`engine._MECHANICS`): THE TWIST / PRECISION / ECONOMY / DEADPAN CONFIDENCE /
  HYPER-SPECIFIC+VERY-ONLINE / ALWAYS SHARP. Same for every profile. Round-3 grounding (2026-07-05,
  range-neutral craft only): PRECISION carries the LITERAL-READ demand (grant any premise, but numbers
  compute / comparisons map / payoff follows — ~9/18 round-3 kills were mechanism breaks) + TRUE double
  meaning as the named win mechanism (two-DUIs 8, "still getting bread" punch-up); HYPER-SPECIFIC
  carries in-world specifics as a POSITIVE extension (the LED-sign lesson: "the randomness has a place
  110%, but the thing has to be within the voice" — never shipped as a negative rule).
- **Per-profile PERSONA** (`var/profiles/<id>/persona.md`, GET/POST `/api/profiles/{id}/persona`) +
  the profile's own `references.jsonl`. `voice_system() = persona + references + _MECHANICS`.
- **VOICE POINTER**: a profile can generate with ANY profile's voice. `profiles.voice_id()` reads
  `var/profiles/<id>/voice.json` (default: own voice). VOICE-owned files (corpus, persona, ref_usage,
  ref_scores, grades, genlog, taste) resolve through the pointer when pid is None; PROFILE-owned files
  (reels, drive export) stay with the profile. Each reel records its `voice_profile_id` — grading
  keep-credits and learn/promotion flow into the VOICE that generated it. API: `GET /api/voices`
  (label = User.voice_label or handle; profiles without a corpus are hidden), `POST /api/voice`
  {voice_profile_id} for the active profile, `POST /api/profiles/{id}/voice-label` to rename a voice's
  display (Austin's voice is labeled **"Base"**). UI: the Generation Studio voice cards toggle it.
- New same-archetype profile: seed via `POST /api/profiles/{id}/bootstrap-voice {verbatim:true}`
  (copies source originals as-is, drops gambling refs + later promotions). Different archetype:
  `verbatim:false` LLM-reskins (how Check was made).
- Retiring a reference: add its EXACT CAPTION to `app/corpus/retire.py` RETIRED_CAPTIONS (boot purge
  cleans every profile; ids are profile-local and renumbered — never match by id).

## Generation → selection → learning (the pipeline)

- **⭐⭐ THE REGROUND (2026-07-08, commit 5f51dee) — generation rebuilt from the operator's NORTH
  STARS.** Operator supplied 5 gold-standard captions from the wild ("the overall voice and framing,
  and THE POINT — the actual premise of what it's saying") after judging our output over-crafted.
  What the north stars teach: every caption SAYS something statable in one sentence (truth /
  straight-faced delusion / coded take); SAID not written (zero punchline architecture — the wit is
  invisible); the READER finishes it (recognition, decode, hidden math like 10×0=0); STANCE mix
  (observer "mfs/bro/men" pointing ≈ performer bits). Built: **north-star tier**
  (var/north_stars.jsonl, /api/northstars, 5 seeded with decoded points — THE BAR in both
  generation stages, premises taken); **VOICE CORE** (var/voice_core.md via /api/voice-core —
  operator-editable taste document replacing MY accreted TWIST/PRECISION/snap language in v2 paths;
  ≥100-char guard); **point-first two-stage** (Stage A ideates plain-sentence POINTS + you/pointing
  stance — the "premise+play" shape-language produced fan-fiction and over-crafting; Stage B "type
  it the way you'd actually type it" at the wall+bar). Verified live: energy-drinks-class
  observations on fresh premises, zero morphs, zero crafted-wordplay tells. Plan remainder: P3
  footage-reactive caption mode (hero clip context into ideation — 2 of 5 north stars are ABOUT
  their footage; the clip-aware lane exists unused), P4 chooser bar = north stars (eval-gated), P5
  rapid text-only taste loops on /grade between reel rounds.
- **⭐ GENERATION = v2 UNDERSTANDING-FIRST (2026-07-07, commit fbe1774, operator directive: "orient
  the system for SUCCESS, not to follow a list of rules… stop morphing the catalog").** Production
  (`engine._generate_v2`, both batch + reel paths) now runs the lab's operator-corrected two-stage:
  Stage A IDEATES premise+play pairs **as the catalog's author — v1's exact voice system (persona +
  full reference wall + mechanics)** with the catalog + recent output as TAKEN territory: the
  references carry the voice INTO the ideas (v2.1 operator correction: codex-only ideation "loses
  so much alignment and voice" — a description of greatness is lossy, re-learned in production),
  while taken-territory keeps premises fresh and no anchor duty exists to morph. Stage B EXECUTES
  with the wall as BAR + sound-check (premises locked; codex rides as understanding). Same curation
  downstream (regurgitation drop → refine → sonnet chooser); the regurgitation guard gained a
  MORPH tier (marker-stripped content containment ≥ .62 — catches seagulls/pigeons noun-swaps,
  keeps frame species' legitimate skeletons); ideation retries once on truncated JSON. Execution
  tells named in the shared prompts (operator calibration): matter-of-fact decode landings and
  NARRATED fan-fiction scenes are dead on arrival — drop the reader in, never perform.
  Candidates carry EMPTY anchor_refs — grade attribution/rotation are v1 concepts; **the v2 loop is:
  grade → learn → corpus promotions + note mining → CODEX force-rebuild (now automatic in
  /api/reels/learn) → next generation ideates from the updated understanding.** Rollback:
  `GENERATION_ENGINE=v1`. Verified live: fresh premises, double-meaning-rich, zero catalog morphs
  (frame-species word overlap on wyr is the species, not a morph). The v1 machinery below
  (rotation/anchors/species floor/quality offsets) remains for the rollback path only.
- **Generation v1** (`app/caption/engine.py`): rotation-anchored — each candidate sparked by a distinct
  reference (least-used-first, grade-weighted: winners recur, chronic-miss refs de-weighted via +3
  virtual usage, NEVER dropped). Anchors render caption + WHY IT LANDS. Frame anchors (POV/"how bro"/
  dialogue/would-you-rather) keep their SPECIES (never converted to statements). Anti-repeat window:
  `recent_generated(150)` rendered as **9-word PREMISE STUBS, never full captions** — full texts
  were 150 in-prompt length examples and created a measured ratchet (pool drifted 17.5→19.9 mean
  words while refs held ~17; chooser was CLEAN at 0.518 mean length-rank — the 2026-07-04 audit).
  `GET /api/debug/length-audit` = the corpus-vs-pool-vs-chosen length forensics, rerun it before
  blaming any layer for length drift. **Anchor-regurgitation guard**: candidates whose word-set
  containment vs ANY corpus ref ≥ .8 are dropped pre-chooser (round 2 found 3 of 13 "winners" were
  near-verbatim ref copies — an elite anchor comes back as itself and the chooser rightly picks
  it). Gambling anchor cap: ≤1 for batches ≤6. **SPECIES FLOOR** (2026-07-04, operator rule:
  validated species must never just disappear): every batch n≥5 guarantees ≥1 FRAME anchor
  (POV/🥷/wyr/wtf-is/when/how-bro) + ≥1 SINCERE anchor (largest seed cluster, 17/84, but only
  2/47 promotions — the learn loop structurally dilutes it). Avoid stubs are MARKER-STRIPPED
  content stubs + format-neutral wording ("only the IDEA must be new") — raw opener-stubs made
  wyr entries premise-free format prefixes. **PRODUCE-mode slates (2026-07-06, adversary-reviewed;
  commits 6deee02+772bfb7):** the reel path (`generate_independent` → `_pick_anchors(produce=True)`)
  adds posted-rating quality offsets to the rotation sort (last-5 ratings per anchor + batch
  keep/kill rehab at half weight, m=5 shrinkage, combined failer+quality clamp ±3, NO provenance
  pseudo-obs — measured anti-signal: validated-ANCHORED reels mean 4.98 < μ 5.29, an operator-loved
  LINE is not a fertile ANCHOR). Root cause it fixed: the winner reserve was structurally DEAD in
  the reel era (is_winner needed ≥6 keep/kill credits, only ≥8-posted keeps exist → amplified=[]
  live, 240/240 graded slates zero winners — the operator's "alternates are always 1-3, as if
  generated with the intention to not be selected"). is_winner era-fix (≥2 grades, ≥60% keep)
  revived the reserve — the only PERSISTENT amplifier (offsets are entry phase-shifts) — 14 refs
  amplified live. Species floor + reserve apply in BOTH modes (floor slots quality-ordered within
  species in produce); batch/explore path behavior unchanged (its own offset-free sort) = the
  exploration/rehabilitation surface. Probe: `POST /api/debug/slate-probe {k}`.
  **Batch generation is PIPELINED (2026-07-06, commit 7a2d996):** `POST /api/generate/batch {n}` +
  poll `GET /api/generate/batch/{job_id}` — captions run SERIAL (the anti-repeat window and
  rotation usage must see each slate before the next starts; `_USAGE_LOCK` guards the ref_usage
  read-modify-write) while renders (clip-match + ffmpeg) overlap in a pool
  (`reel_render_concurrency=2`). Measured: batch of 3 in 154s vs ~6-9 min sequential. Demo mode
  403s the batch endpoint. UI (app.html) starts the job and polls per-card states. Mix audio now
  routes through caption-first `match_audio` (the client used to pre-pin a random track, which
  silently bypassed audio matching).
  **Voice identity: unemployed is NOT poor** — persona
  rewritten (show-don't-tell wealth; payday/eviction/overdraft/wage-life = not his universe);
  p048 (overdraft-$35, the corpus's only genuinely-poor ref) removed; operator grades under-rate
  poor-coded lines 4.29 vs 5.33. ⚠️ Forensics lesson: the stub-suppression hypothesis was
  REFUTED by measurement (frames flat-to-rising in pool; rotation uniform, zero kills) — POV's
  collapse was ONE stale premise family (pretend-rich parenthetical, rated 3-5) being correctly
  premise-suppressed; ninja generates but the chooser has never picked it (0/14, eval-gated
  note). Reels use best-of-5 independent candidates (`generate_independent(k=5)`), batch grading
  uses `generate(n)`.
- **Selection** (`app/caption/chooser.py`): best-caption-first; per-profile persona injected at call
  time (modular); ONE veto: clearly soft/self-pitying/off-persona. Never judges format/topic/length.
- **Editor** (`app/caption/refine.py`): subtractive-only (trims over-extended tails, strips
  non-load-bearing filler). Never rewrites or adds.
- **Coherence gate = MEASURED NEGATIVE, default OFF** (`engine._coherence_gate`, settings
  `coherence_gate: off|log|drop`, harness `POST /api/debug/gate-check`). Built for round-3's dominant
  kill class (mechanism breaks); replayed against the round's own kills/hits/endorsed/corpus with TWO
  prompt framings: recall 0/9 at clean precision — a joke-charitable judge PARSES those lines fine;
  the operator's "doesn't make sense" is sloppy-MAPPING taste, and strictness high enough to catch it
  flags paradox/absurdist refs first (the distilled-taste-filter failure shape). Don't re-enable
  without a new replay pass; the class is addressed at generation (PRECISION literal-read grounding).
- **Learning** (`/api/reels/learn`, idempotent — re-run until corpus size stable): mines every graded
  reel's note + promotes ≥8 lines into the corpus (`app/corpus/promote.py`, provenance
  promoted_gen/note_endorsed, ref_id p### or renumbered). Railway's edge 502s the long call but the
  WORK CONTINUES server-side — poll `/api/refs/audit` total_refs until stable.
- **Grading UI**: `/grade-reels` (reels, /10 + notes — notes are the PRIMARY signal; the operator
  often quotes a better alt: "X would have been an 8/9" → auto-mined). `/grade` (caption batches,
  keep/kill/off_voice). `/promote` (manual promotion page, now residual — learn auto-promotes).
- **THE LAB** (`/lab` + sidebar tab, `app/caption/lab.py`): **TWO-STAGE — ideate from PRINCIPLES,
  execute at the catalog bar** (operator architecture, v4 after three corrections).
  `build_codex()` consolidates the mechanisms from ALL evidence — every ref's why_it_works, every
  graded reel (8–10 hits AND the operator's 1–4 kill notes AND, since 2026-07-05, noted 5–7
  NEAR-MISSES: that band was a structural dead zone where operator format/template endorsements
  reached nothing — 27 noted mids now feed it), the persona — into a cached
  voice-owned codex (`lab_codex.md`; format taxonomy FORBIDDEN; core/craft/tripwires/8-vs-10).
  Stage A IDEATES premises from the codex with ZERO references in context + catalog premises
  listed as TAKEN → premises structurally can't be re-skins (topic fixed before any ref is seen;
  overgenerates n+4). Stage B EXECUTES with the full reference wall as CRAFT CALIBRATION + bar
  ("premises locked — the catalog shows your range of craft, not templates"), writes the
  strongest n. Rebuild the codex after learn rounds (`POST /api/lab/rebuild-codex` / page button)
  — promoted lab hits carry why_it_works and feed the next codex: understanding compounds.
  ⚠️ THREE CANON LESSONS (operator corrections, all mine): (1) never brief exploration as license
  to miss; (2) wall-grounded lab = format mimicry ("raccoon→pigeon" re-skins); (3) codex-ONLY
  (no wall) = quality collapse AND still re-skins — the codex's few quoted fragments become
  super-attractors, and losing the 131 full-fidelity exemplars drops the craft floor (a
  description of greatness is lossy). WHAT and HOW-WELL must be separated structurally.
  ⚠️ max_tokens: adaptive thinking spends from the same budget — THREE lab calls truncated at
  their exact caps (ledger `out=` == cap is the tell); all now 2600/8000/8000. Isolation:
  own `lab_pool.jsonl`, no prod genlog/rotation/reels; ONE bridge back: ≥8 → corpus
  (`source=lab_promoted`, near-dup guarded); re-grade <8 clears the row's claim. Endpoints:
  `POST /api/lab/generate {n}` · `/api/lab/grade` · `/api/lab/stats` · `/api/lab/rebuild-codex`.

## Reel pipeline

- Caption-first: caption generated (audio-agnostic) → `match_audio` picks the track whose vibe
  amplifies it (Mix mode) → clips matched to caption (`_match_clips_to_caption` fit rank) →
  `select_segments`: softmax sampling over fit+freshness, DISTINCT clips per reel by ID **and by
  LOOK** (Marengo embedding cosine ≥ `CLIP_SIM_THRESHOLD` 0.93 = "same footage"; chain:
  visually-distinct-unused → id-distinct → not-consecutive → pool) → beat-cut → ffmpeg composite.
- Duration scales with the caption: `clamp(1.8 + words/3, 5, 9)` seconds; audio fades out.
- **Frozen/duplicate-shot hardening (2026-07-04, four layers — don't remove any):** (1) QC records
  the VIDEO STREAM's real duration, never the container's (phone audio outlives the last frame →
  phantom segments → cuts render zero frames → reel freezes under the audio); (2) `build_slot_plan`
  splits any slot > `reel_max_shot` (3.2s) — a beatless audio stretch once produced ONE 6.6s slot
  no clip could fill (2.1s of video, 4.5s frozen); (3) `select_segments` clamps windows to real
  footage, floors out phantom/near-black/blur segments (tiered, never empties), and de-dups by
  SUBJECT fingerprint (distinctive summary words) — two different clips starring the same subject
  (iced-watch macro ×2, embedding cosine 0.06!) read as "the same clip twice"; embeddings can't
  catch that; (4) compositor tpad(clone)+trim per shot — video can never end before the audio.
  Maintenance: `POST /api/debug/repair-durations` (dry default; fixed 4 clips / 268 on first run),
  `GET /api/debug/clip-probe?ids=|reel_id=` (db-vs-stream durations, segment reach, embedding
  state, per-segment quality). Caption-fit ranking now offers the ranker the top-160 clips by
  quality and falls back to QUALITY order (was: arbitrary 40 + insertion order).
- Caption PNG: TikTok Sans weight 800 via variation axes, Pilmoji color emoji (offline Noto fallback).
- Embedding health: `GET /api/debug/clip-sim` (distribution + top pairs; `?ids=` verifies a reel's
  picks). `POST /api/debug/re-embed` repairs exact-duplicate vectors (`?dry=true` diagnoses).
  KNOWN RESIDUE: ~10 Austin clips return a constant vector from TL even at correct mime — safe
  (mutually-"identical" → ≤1 per reel ever picked). Root cause of the original corruption: uploads
  were sent as video/mp4 regardless of extension; FIXED via mimetypes.guess_type (.mov =
  video/quicktime) in `app/indexing/twelvelabs.py`.

## Google Drive (both directions)

- **Ingest (read)**: service account `treelz-ingest@treelz.iam.gserviceaccount.com` (key in Railway
  env `GOOGLE_SA_JSON`). Creator shares a folder as Viewer → connect in Clip Library UI or
  `POST /api/drive/connect {folder}` → `POST /api/drive/sync/{connection_id}`. Shortest-first
  (durationMillis, size fallback), `SYNC_MAX_CLIP_SECONDS=20` cap (longer clips stay unledgered —
  raise the env + resync to pull them later), 50 files/pass (re-kick until done — see autokick
  pattern below), SyncedFile ledger = incremental/idempotent, failed files do NOT retry.
- **Export (write)**: SAs can't own files in a personal My Drive → exports run as the operator via
  OAuth refresh token (env `GOOGLE_OAUTH_CLIENT_ID/_SECRET/_REFRESH_TOKEN`, scope `drive.file` =
  app-created files only). ✓ Validate in the studio uploads the mp4 (only — no sidecars) to
  "treelz exports/<profile>" in the operator's Drive and returns the link. Folders auto-create per
  profile, self-heal if deleted. OAuth consent app ("Treelz", GCP project treelz) must stay PUBLISHED
  to Production (Testing-mode refresh tokens die every 7 days).
- **Indexing concurrency** (measured): TL processes ~3 tasks simultaneously on this account; excess
  queues server-side free, zero 429s. `INDEX_CONCURRENCY=6` in-flight saturates it (cv2 stages
  serialize on a 1-slot lock inside the pipeline for memory). Wider than ~6 adds nothing.

## Front-end design system (v2 — 2026-07-04 workbench rebuild)

- **`app/static/ui.css` is the single source of truth** — very dark neutral base, ONE accent:
  the **blue→purple gradient** (`--grad` fills, solid `--acc:#7d7bff` for borders/text; white ink
  on gradient fills). SUCCESS/keep/high-ratings stay GREEN (`--good`), off-voice is TEAL —
  semantic colors never reuse the accent. Buttons (`.btn` + `-primary/-soft/-ghost/-danger/-sm/
  -block`), cards, badges, chips, toasts, modals, progress/skeletons, empty states, focus rings.
  ALL six pages link `/assets/ui.css?v=N` — **bump N on change**. No per-page palettes; glow only
  on CTA/focus/selection/live states.
- **App shell = workbench framed on the real workflow**: slim nav sidebar → sticky `.topbar`
  with the profile switcher + LIVE stat chips (indexed clips · reels **to grade** (click→grading)
  · active-voice refs · Drive sync dot) polling the same endpoints the operator watches →
  Studio view is `.wb`: a sticky 308px control rail (compact voice rows, audio, source, notes,
  count, CTA) + a full-width reel canvas where each run lands as a `.batch` (header: count ·
  audio · time) of vertical 9:16 `.rcard`s (video-first, caption rail, validate→Drive/download;
  progress/queued/failed states share the same footprint). Library: folder rail + compact Drive
  strip + slim upload bar (whole view is a dropzone) + dense clip grid.
- Grading pages render INSIDE app iframes — same background, invisible seam; in-iframe links
  that should escape use `target="_top"`. Reel grading = segmented 1–10 (1–4 red / 5–7 neutral /
  8–10 green), same `{reel_id, rating, notes}` contract.
- **Design-review harness**: `node tools/design_preview.cjs` → http://localhost:4173 serves the
  real static pages with stubbed APIs + fixture media (regen instructions in the file header) —
  QA every page and state with zero prod risk. Gotcha: in an occluded/background preview tab,
  CSS transitions/animations FREEZE mid-flight — computed styles can read stale mid-transition
  values; disable the element's transition before asserting colors.

## The friends demo (DEMO_MODE — second Railway service, same repo)

- **https://trial-studio-demo-production.up.railway.app** · service `Trial-Studio-Demo` +
  `Postgres-kTDA` + volume `trial-studio-demo-volume:/app/var`, all CLI-provisioned; every push
  deploys prod AND demo. Full runbook: [docs/DEMO.md](docs/DEMO.md).
- `DEMO_MODE=1` flips the service: open signup (User rows w/ pbkdf2 `password_hash`; each account
  IS a profile, bound per-request via `profiles.set_request_uid` ContextVar — the global
  active-profile file is single-operator state and is bypassed), route WHITELIST (operator pages +
  debug endpoints 404), mobile-first `demo.html` at `/`. Voice = the shared Base profile seeded
  from `corpus/demo_base/` (exported Austin corpus). Caps: 50 clips / 30s each / 15 reels →
  24h cooldown → full reset (failures never consume; quota in `demo_quota.json` per profile).
- Prod runs DEMO dormant — verified: demo endpoints 404, all operator surfaces intact.
- The demo page is a 3-STAGE WIZARD (add clips → we get them ready → make reels; stepper unlocks
  live, one primary action per stage, no jargon). **/admin** = the operator dashboard (accounts
  table, stat cards, playable latest-reels feed) gated on the OPERATOR cookie — demo sessions
  can't pass; demo-service operator creds are env-set non-defaults (password in local
  tmp/demo_admin_pw.txt, never committed).

## LLM cost discipline (2026-07-04 — measured, zero quality change)

- **Prompt caching** on the byte-identical system prompts: the k=5 reel candidates (and Lab
  collisions) run **SEQUENTIAL-FIRST** — candidate 1 alone pays the single 1.25× cache write, then
  the fan-out reads at ~10% (parallel fan-outs RACE the cache: 5 simultaneous calls = 5 writes,
  0 reads — measured; a primer call isn't propagated in time either, 1/5 reads — measured).
  chooser + refine systems are stable → cross-reel cache hits through a sequential batch. NOT
  marked: batch-grading `generate(n)` (system reshuffles per call → write surcharge for nothing),
  tiny match/audio systems (below the 1024-token minimum). Verified live: 1×cache_w=5094 then
  4×cache_r=5094 → system-input spend −67% (≈ −$0.085/reel), total input −~38%.
- Every Anthropic call prints `[llm] tag=<call-site> <model> eff= in= out= cache_w= cache_r=` to
  stdout → Railway logs are the permanent per-call-site cost ledger. Measured reel profile
  (2026-07-04): candidates 72% · clip-match 21% · refine 4% · chooser 2% · audio-match 1%,
  total ≈ $0.33 (pre-clip-cache).
- **clip-match clip LISTING is a cached user-prefix block** (`cache_user_prefix` in complete_json):
  the 11.7k-token clip list is byte-stable between reels (deterministic order: quality desc, id
  tiebreak — REQUIRED, an unordered DB read would shuffle ties and miss) while only the caption
  tail varies. Verified live: reel 1 `cache_w=11716`, reel 2 `cache_r=11716` → matcher
  ~$0.069→~$0.016/reel in batches; an isolated reel >5 min after the last pays the 1.25× write
  (+$0.015). Input change acknowledged: the ranker sees clips-before-caption now (mechanical
  call). In-batch steady state ≈ **$0.28/reel total (~35% below pre-caching)**.
- Quality-bearing levers deliberately untouched: model (Opus), effort tiers, adaptive thinking,
  the per-reel corpus shuffle (stabilizing it would enable cross-reel candidate caching but
  changes generation inputs — needs a measured A/B, don't do silently).

## Ops runbook

- **Deploy** = push to main (Railway auto-builds). `railway` CLI is linked from this directory
  (project dynamic-emotion / service Trial_Studio). Env var changes trigger their own redeploy
  (`railway redeploy` after a var-set often exits 1 — harmless collision).
- **⚠ Deploys KILL in-flight background workers** (drive syncs, re-embeds, learn runs). Sync claims
  are swept at boot (stale 'syncing' → 'connected'); re-kick after deploying. Never deploy mid-repair.
- **Long server jobs** (learn, re-embed): the edge 502s ~60-120s but the worker continues — poll the
  relevant counter until stable, re-run (idempotent) to continue.
- **Autokick pattern** (big Drive folder): loop { poll /api/drive/status; if connected → kick
  /api/drive/sync/{id} }; complete after 3 consecutive kicked polls with zero files_seen growth.
- **Health**: `GET /health` returns the deployed commit sha — poll it after pushing.
- Local WSL: `cd trial-studio && .venv/bin/python ...`; git push works from Windows PowerShell
  (`git -C C:\Users\Streaming\trial-studio push`) — root WSL has no GitHub creds.
- Useful debug endpoints: `/api/refs/audit` (corpus size + retired check), `/api/refs/rotation`
  (per-ref keep/kill/usage/status), `/api/chooser/eval` (selection benchmark), `/api/debug/clip-sim`,
  `/api/debug/re-embed`, `/api/reels/{pending,graded,learn}`, `/api/drive/status`.
- Corpus maintenance: `POST /api/debug/corpus-dedup?dry=true|false` (near-dup twins, keeps earliest)
  + `POST /api/debug/corpus-remove {ref_ids}` (operator-directed same-joke consolidation — used
  2026-07-04 to tune down two over-represented Austin families: equivalence 6→4 refs, status-burn
  5→3, each keeping its best exemplars; promote._add_ref now near-dup-guards future promotions so
  one joke never stacks multiple corpus slots).

## Environment variables (values live in Railway — NEVER commit them; repo is public)

See `.env.example` for the full list of names. Key ones: `DATABASE_URL`, `ANTHROPIC_API_KEY`
(captions, claude-opus-4-8), `TWELVELABS_API_KEY`, `OPENAI_API_KEY` (test backend only),
R2_* (Cloudflare storage), `GOOGLE_SA_JSON` (Drive ingest), `GOOGLE_OAUTH_CLIENT_ID/_SECRET/
_REFRESH_TOKEN` (Drive export), knobs: `INDEX_CONCURRENCY` (6), `SYNC_MAX_CLIP_SECONDS` (20),
`CLIP_SIM_THRESHOLD` (0.93).

## Profiles + current state (as of 2026-07-03)

- **Spence** (first profile): the original voice — young terminally-online get-rich guy; gambling is
  ONE flavor (10% of refs, persona names it). 153 refs (94 originals + promotions), ~190 graded reels.
- **Austin** (`1743bd43-…`): the BASE voice = Spence minus gambling emphasis (persona has no gambling
  clause; verbatim-seeded 84 non-gambling originals). 224+ indexed clips (215 Drive-synced). Round 1:
  136 reels graded, 21% ≥8 / 38% ≤4. Round 2 (2026-07-04, 37 reels): **35% ≥8 (29% organic after
  excluding 3 ref regurgitations) / 38% ≤4, mean 5.43→5.86**; zero length/clip/audio/off-voice
  complaints; learn promoted 10+1 → corpus 131 refs. Dominant miss theme is STILL "decent premise,
  flat delivery/landing" (8 of 14 noted misses) — standing levers are winner-promotion grounding +
  kill attribution; the craft-deepening prompt experiment was A/B-refuted, don't re-add it.
- **Check**: 40 bootstrap-reskinned refs, no clips/grades yet.
- Test rig (dormant): `?backend=sonnet|openai` isolates a full pipeline on another model
  (claude-sonnet-5 / gpt-5.5) with suffixed state files. Operator verdict: both worse than Opus.

## Working style expectations (the operator's standing directions)

- Improve the CORE GENERATOR; don't overcomplicate with mechanism sprawl. If the generator gets
  better, everything gets better.
- Verify everything live (deploy → run → show real output/numbers). Never claim without measuring.
- Feedback rounds: pull `/api/reels/graded`, analyze themes with counts, map each to the RIGHT layer
  with evidence, apply the canon, run learn, verify with a fresh pool.
