# The friends demo (DEMO_MODE)

**URL: https://trial-studio-demo-production.up.railway.app** — send this link.

Same repo as production, second Railway service (`Trial-Studio-Demo` in project
`dynamic-emotion`) with `DEMO_MODE=1`. Own Postgres (`Postgres-kTDA`) + own volume
(`trial-studio-demo-volume` at `/app/var`) — friends' data never touches prod. Every
`git push` to main deploys BOTH services.

## The friend flow
1. Open the link → create a username + password (open signup, no invite code).
2. Upload clips from the camera roll (50 max, 30s max each — longer clips are
   QC-rejected with a clear reason; rejected clips don't count against the cap).
3. Clips index automatically (TwelveLabs; a minute or two each, live status tiles).
4. Tap **Generate a reel** — caption in the Base voice, clips matched + beat-cut,
   audio auto-matched to the line. Download per reel.

## Limits
- **15 reels per user**, then a **24h cooldown**, then the counter fully resets.
  Failed generations never consume quota. Countdown shows in the UI.
- Caps are env-tunable on the demo service: `DEMO_MAX_CLIPS`, `DEMO_MAX_CLIP_SECONDS`,
  `DEMO_REELS_PER_WINDOW`, `DEMO_COOLDOWN_HOURS`.

## What demo users can reach (whitelist — everything else 404s)
The demo page, demo auth/status/reels APIs, clip upload/library/status/thumb/delete,
`/api/generate`, and their OWN reel files. All operator surfaces (studio, grading, lab,
templates, drive, debug endpoints, corpus tooling) are unreachable on the demo domain.

## Architecture notes
- Each signup = a `User` row (pbkdf2 password hash) = its own PROFILE; the demo
  middleware binds the session to `profiles.active_id()` per request (ContextVar), so
  all existing per-profile scoping just works. Signups point their voice at the shared
  **Base voice** profile, seeded at boot from `corpus/demo_base/` (exported Austin
  corpus, 131 refs + persona). Audio library seeds itself on first boot.
- Demo generation shares the Base voice's anti-repeat/rotation state across all demo
  users (by design — variety across the whole demo). Nothing feeds back into any
  corpus (no grading surface exists in the demo).
- Costs land on the prod API keys (copied at provisioning): TwelveLabs per indexed
  minute, Anthropic per reel (~6 Opus calls). The clip caps bound TL; the reel quota
  bounds Anthropic.

## Admin dashboard

**https://trial-studio-demo-production.up.railway.app/admin** — operator-only (env
`TREELZ_USER`/`TREELZ_PASSWORD` on the demo service; set to non-defaults since the repo
defaults are public). Shows: stat cards (accounts, reels made, reels/24h, clips
ready/rejected, cooldowns), a per-account table (joined, clip counts, window + lifetime
reels, last-reel age, status), and a playable feed of the latest reels across all
accounts with captions. Auto-refreshes every 60s. Demo sessions can never pass the
admin gate (it requires the operator cookie, a different auth system).

## Ops
- Health: `GET /health` (returns the deployed commit).
- Re-verify end-to-end after any change: `bash tmp/demo_live_verify.sh` (WSL) — signs
  up a throwaway user, uploads fixtures, indexes, generates, checks quota + whitelist.
- Reset a friend's quota manually: delete `var/profiles/<their id>/demo_quota.json`
  on the demo volume (`railway volume files` or a redeploy won't touch it).
- The demo service was provisioned entirely via CLI:
  `railway add --database postgres` · `railway add --service --repo --variables` ·
  `railway volume --service <id> add -m /app/var` · `railway domain`.
