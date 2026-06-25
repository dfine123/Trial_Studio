# Deploying treelz.ai to Railway

Everything's packaged. Your part is ~10 minutes of clicking. (No Redis, no separate worker —
clip indexing runs in-process, and a persistent volume holds your media + grades.)

## 0. The code is on GitHub
The repo `github.com/dfine123/Trial_Studio` has the latest (Dockerfile, boot script, all of it).
Later changes: just `git push` and Railway auto-redeploys.

## 1. Create the project
1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Pick **dfine123/Trial_Studio**. Railway sees the **Dockerfile** and starts building.

## 2. Add a Postgres database
1. In the project → **New** → **Database** → **PostgreSQL**.
2. On the **web service** → **Variables** → add `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   (that `${{...}}` is Railway's reference syntax — it wires the app to the DB).

## 3. Add a Volume (so clips, reels, and grades persist across deploys)
- Web service → **Settings** → **Volumes** → **New Volume**, mount path: **`/app/var`**

## 4. Set environment variables
Web service → **Variables** (full list in `.env.example`):
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TWELVELABS_API_KEY` — your keys
- `TREELZ_USER` = `dfine`, `TREELZ_PASSWORD` = *(your choice)*, `TREELZ_SECRET` = *(a long random string)*
- R2 + Redis vars can stay empty.

## 5. Deploy
Railway builds + boots. **First boot takes a few minutes** — it creates the tables, seeds the
13 audios (beat analysis on each), and restores your grading history. Watch **Deploy → Logs**;
once the healthcheck (`/health`) passes, the URL goes live.

## 6. Use it
Open the generated URL → log in (`dfine` / your password). Studio, Clip Library, and Grading
all work. **Re-upload your clips** in the Library — they were local files, so they index fresh
in the cloud (with progress bars).

---

### Good to know
- **Rendering is CPU-heavy** (ffmpeg 4K→1080p). On the starter plan a reel can take a few
  minutes; bump the service's CPU/RAM in Settings if it drags.
- **What's seeded vs. fresh:** your voice references + grading history (≈860 verdicts) ship in
  the image and seed on first boot. Audios are re-analyzed automatically. Clips need re-uploading.
- **Custom domain:** Railway → service → Settings → Networking → add `treelz.ai` and point your
  DNS at the Railway target once you're ready.
- **Manage it:** Deploys/Logs in the dashboard; `git push` to redeploy. Migrations later can use
  the bundled alembic, but a fresh DB is created automatically on boot.
