# Deploying Trial Studio (Phase 0) to Railway

Railway runs Linux with CPU FFmpeg — the same environment we built against locally, so this
is mostly clicking through the dashboard + pasting variables. Do this **after** local
acceptance passes.

## What you'll create
**One service** from the repo — the Dockerfile runs DB migrations, the background indexer,
and the API together — plus the **Postgres** and **Redis** plugins.
(For higher throughput later you can split the indexer into its own service with start
command `python -m app.workers.run`; not needed for Phase 0.)

## Steps

1. **Push the code to GitHub** (I do this once you give me the repo URL).

2. **Create the Railway project** → add the **PostgreSQL** plugin and the **Redis** plugin
   (New → Database → Postgres, then again for Redis).

3. **Create the `web` service** → Deploy from your GitHub repo. Railway detects the
   `Dockerfile` automatically. No start command needed (the Dockerfile runs
   `alembic upgrade head && uvicorn …`).

4. *(No separate worker service needed — the single service already runs the indexer in the
   background. Leave the Start Command blank so it uses the Dockerfile's built-in command.)*

5. **Set environment variables on BOTH services** (Variables tab). Reference the plugins for
   the first two; paste the rest from your local `.env`:
   ```
   DATABASE_URL = ${{Postgres.DATABASE_URL}}
   REDIS_URL    = ${{Redis.REDIS_URL}}
   ANTHROPIC_API_KEY      = (from .env)
   TWELVELABS_API_KEY     = (from .env)
   R2_ACCOUNT_ID          = (from .env)
   R2_ACCESS_KEY_ID       = (from .env)
   R2_SECRET_ACCESS_KEY   = (from .env)
   R2_BUCKET_NAME         = trial-studio
   R2_ENDPOINT            = (from .env)
   ```
   (Plugin variable names may render as `${{Postgres.DATABASE_URL}}` in the picker — use
   Railway's "Add Reference" button.)

6. **Deploy.** The `web` service applies migrations on boot; the `worker` starts consuming
   the `indexing` queue. Open the `web` service's public URL and hit `/health` — it should
   report `"database": true`.

7. **Smoke test in the cloud**: `POST /clips` with a small clip → poll `GET /clips/{id}`
   until `status=indexed`.

## Notes
- Secrets are **not** in the image (`.dockerignore` excludes `.env`); Railway injects them.
- Scale throughput later by adding more `worker` replicas (Phase 4).
- Keep one `web` replica for now so migrations don't race; the worker can scale freely.
