# Deploying treelz.ai to Railway (CLI — keeps your voice OFF GitHub)

Your repo stays public, but your voice references + grading data **never touch GitHub** — they
upload straight from this machine to Railway via the CLI (`--no-gitignore` reads `.railwayignore`
instead of `.gitignore`, so the gitignored voice files DO ship to Railway but not to git).

Run everything below from **`C:\Users\Streaming\trial-studio`** (PowerShell).

## 1. Railway CLI
Installed for you — check with `railway --version`. If missing: `npm i -g @railway/cli`

## 2. Log in + create the project
```
railway login          # opens your browser to authenticate
railway init           # name it "treelz" — creates the project
```

## 3. First deploy (uploads EVERYTHING incl. your voice, ignoring .gitignore)
```
railway up --no-gitignore
```
Railway builds the Dockerfile (first build = a few minutes) and creates the service. It'll boot
but report DB errors until step 4 — expected.

## 4. Add database, volume, and secrets — Railway dashboard (railway.app → your project)
- **Postgres:** New → Database → PostgreSQL. Then on the **service → Variables**, add
  `DATABASE_URL = ${{Postgres.DATABASE_URL}}`
- **Volume** (persists clips/reels/grades across deploys): service → Settings → Volumes →
  New Volume, mount path **`/app/var`**
- **Variables** (service → Variables):
  - `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TWELVELABS_API_KEY` — your keys
  - `TREELZ_USER` = `dfine`, `TREELZ_PASSWORD` = *(your choice)*, `TREELZ_SECRET` = *(long random string)*

## 5. Redeploy + go live
```
railway up --no-gitignore
railway domain         # generates a public https URL
```
Open the URL → log in. First boot seeds the audios + restores your grading history (a few minutes —
watch `railway logs`).

## 6. Use it
Studio + Clip Library + Grading all work. Re-upload your clips in the Library (they index in the
cloud now, with progress bars).

---
### Redeploy later
From the project dir: **`railway up --no-gitignore`** (always include the flag so your voice ships).

### Notes
- **Rendering** (ffmpeg 4K→1080p) is CPU-heavy — bump the service's CPU/RAM in Settings if reels drag.
- Your voice + grades live only in the Railway image/volume and on this machine — **never on GitHub**.
- `railway logs` = live logs · `railway open` = dashboard · `railway domain` = the public URL.
- Custom domain (treelz.ai): service → Settings → Networking → Custom Domain, then point your DNS.
