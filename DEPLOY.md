# Deploying treelz.ai to Railway (from GitHub — no CLI)

Everything's on GitHub: github.com/dfine123/Trial_Studio. Your part is ~10 minutes of clicks.

## 1. Create the project
1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Authorize Railway's GitHub access if asked, then pick **dfine123/Trial_Studio**.
3. Railway detects the **Dockerfile** and starts building (first build = a few minutes).

## 2. Add Postgres
- **New → Database → PostgreSQL.** Then on the web service → **Variables** → add
  `DATABASE_URL = ${{Postgres.DATABASE_URL}}`

## 3. Add a Volume (persists clips/reels/grades)
- Web service → **Settings → Volumes** → New Volume, mount path **`/app/var`**

## 4. Set environment variables (service → Variables)
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TWELVELABS_API_KEY`
- `TREELZ_USER` = `dfine`, `TREELZ_PASSWORD` = *(your choice)*, `TREELZ_SECRET` = *(long random string)*

## 5. Deploy + go live
- Railway redeploys when you add the DB / volume / variables. **First boot takes a few minutes**
  (creates tables, seeds the audios + your grading history) — watch **Deploy → Logs**.
- Service → **Settings → Networking → Generate Domain** → that's your public URL.

## 6. Use it
Open the URL → log in (`dfine` / your password). Studio + Clip Library + Grading all work.
Re-upload your clips in the Library (they index in the cloud now).

### Notes
- **Rendering** (ffmpeg 4K→1080p) is CPU-heavy — bump the service's CPU/RAM in Settings if reels drag.
- **Redeploy:** just `git push` — Railway auto-redeploys from GitHub.
- **Custom domain** (treelz.ai): Settings → Networking → Custom Domain → point your DNS at the target.
