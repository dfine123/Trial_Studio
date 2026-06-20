# Trial Studio — Indexing (Phase 0)

The foundation + clip-indexing pipeline for Trial Studio. **Phase 0 scope only:**
upload a clip → store in R2 → quality-gate → segment → analyze (Twelve Labs + OpenCV)
→ persist a full index record; plus an audio-seed script with librosa beat maps.

> Out of scope for Phase 0 (later phases): rendering/compositing, caption generation,
> clip sequencing, the Generate endpoint, and any web UI.

## What it does
- `POST /clips` — accept a video, store to R2 (user-scoped key), enqueue async indexing.
- Indexing worker — QC gate (reject < 1080p / < 30fps) → PySceneDetect + long-take
  windowing → Twelve Labs (Pegasus summary/tags + Marengo embedding) → OpenCV per-segment
  metrics + usability → persist `Clip` + `Segment` rows, `status=indexed`.
- `GET /clips`, `GET /clips/{id}` — read the index record (clip + its segments).
- `seed/seed_audio.py` — load test audios with a librosa `beat_map`, manual `beat_drop_ts`,
  and `structure`.

## Stack
Python 3.12 · FastAPI · SQLAlchemy 2 + Alembic · RQ (Redis) · boto3 (R2) ·
ffprobe/ffmpeg · PySceneDetect · OpenCV · librosa · Twelve Labs SDK.

## Local dev (this machine = Windows + WSL/Ubuntu)
Postgres 18, Redis 8, and FFmpeg run inside WSL Ubuntu (no Docker/admin needed); the app
runs there too. The repo lives on the Windows drive at `C:\Users\Streaming\trial-studio`
(= `/mnt/c/Users/Streaming/trial-studio` inside WSL).

```bash
# inside WSL (Ubuntu)
cd /mnt/c/Users/Streaming/trial-studio
source .venv/bin/activate
alembic upgrade head
uvicorn app.main:app --reload --port 8000      # API
rq worker indexing                              # worker (separate shell)
python -m app.seed.seed_audio                   # seed audios
```

Services: Postgres `localhost:5432` (db `trial_studio`, role `trial`), Redis `localhost:6379`.
Config is read from `.env` (see `.env.example`). **Never commit `.env`.**

## Deploy
Railway (CPU, FFmpeg in the image) — provisioned as the final step, after local
acceptance tests pass. See deploy notes at the end of Phase 0.
