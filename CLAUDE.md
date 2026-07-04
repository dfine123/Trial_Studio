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
   it seemed (this caught a real one).
4. **Measure corpus-vs-pool-vs-chosen before assigning a drift to a layer.** A "chooser problem" was
   generation-side twice (frame loss, length). Generate a raw pool and compare distributions first.
5. **Turn voice elements up/down via POSITIVE priming only** (persona slang list, reference mix,
   structural caps) — never "don't do X". Precedents: 🥷 emoji (removed from persona slang list, stays
   in refs), gambling (10% of refs is honest; anchor cap scales with batch size).
6. **The corpus IS the generator's brain, and grades feed it automatically.** `/api/reels/learn`
   mines notes (pairwise + off_voice) AND auto-promotes every operator-validated line (posted reels
   rated ≥8 + note-endorsed "would have been an 8/9" alts) into the profile's references with decoded
   why_it_works. Grade → learn → better generator. That's the whole loop.

## Voice architecture (two layers; voices are TOGGLEABLE per profile)

- **Shared FORMAT base** (`engine._MECHANICS`): THE TWIST / PRECISION / ECONOMY / DEADPAN CONFIDENCE /
  HYPER-SPECIFIC+VERY-ONLINE / ALWAYS SHARP. Same for every profile.
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

- **Generation** (`app/caption/engine.py`): rotation-anchored — each candidate sparked by a distinct
  reference (least-used-first, grade-weighted: winners recur, chronic-miss refs de-weighted via +3
  virtual usage, NEVER dropped). Anchors render caption + WHY IT LANDS. Frame anchors (POV/"how bro"/
  dialogue/would-you-rather) keep their SPECIES (never converted to statements). Anti-repeat window:
  `recent_generated(150)` rendered as **9-word PREMISE STUBS, never full captions** — full texts
  were 150 in-prompt length examples and created a measured ratchet (pool drifted 17.5→19.9 mean
  words while refs held ~17; chooser was CLEAN at 0.518 mean length-rank — the 2026-07-04 audit).
  `GET /api/debug/length-audit` = the corpus-vs-pool-vs-chosen length forensics, rerun it before
  blaming any layer for length drift. Gambling anchor cap: ≤1 for batches ≤6. Reels use best-of-5
  independent candidates (`generate_independent(k=5)`), batch grading uses `generate(n)`.
- **Selection** (`app/caption/chooser.py`): best-caption-first; per-profile persona injected at call
  time (modular); ONE veto: clearly soft/self-pitying/off-persona. Never judges format/topic/length.
- **Editor** (`app/caption/refine.py`): subtractive-only (trims over-extended tails, strips
  non-load-bearing filler). Never rewrites or adds.
- **Learning** (`/api/reels/learn`, idempotent — re-run until corpus size stable): mines every graded
  reel's note + promotes ≥8 lines into the corpus (`app/corpus/promote.py`, provenance
  promoted_gen/note_endorsed, ref_id p### or renumbered). Railway's edge 502s the long call but the
  WORK CONTINUES server-side — poll `/api/refs/audit` total_refs until stable.
- **Grading UI**: `/grade-reels` (reels, /10 + notes — notes are the PRIMARY signal; the operator
  often quotes a better alt: "X would have been an 8/9" → auto-mined). `/grade` (caption batches,
  keep/kill/off_voice). `/promote` (manual promotion page, now residual — learn auto-promotes).

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
  clause; verbatim-seeded 84 non-gambling originals). 224 indexed clips (215 Drive-synced). Round 1:
  136 reels graded, 23% ≥8 / 41% ≤4 (base voice confirmed working, zero gambling/off-voice notes);
  learn promoted him to 124 refs. Dominant remaining quality theme: "good premise, flat FINAL BEAT"
  — being attacked via promotion grounding; if it persists next round, run a measured experiment on
  landings (blind-panel A/B methodology, never a prompt rule).
- **Check**: 40 bootstrap-reskinned refs, no clips/grades yet.
- Test rig (dormant): `?backend=sonnet|openai` isolates a full pipeline on another model
  (claude-sonnet-5 / gpt-5.5) with suffixed state files. Operator verdict: both worse than Opus.

## Working style expectations (the operator's standing directions)

- Improve the CORE GENERATOR; don't overcomplicate with mechanism sprawl. If the generator gets
  better, everything gets better.
- Verify everything live (deploy → run → show real output/numbers). Never claim without measuring.
- Feedback rounds: pull `/api/reels/graded`, analyze themes with counts, map each to the RIGHT layer
  with evidence, apply the canon, run learn, verify with a fresh pool.
