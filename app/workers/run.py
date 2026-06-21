"""Worker entrypoint:  python -m app.workers.run

Starts an RQ worker on the `indexing` queue using REDIS_URL from settings (so it works on
Railway without relying on the rq CLI's localhost default or shell var-expansion).
Logs a masked view of the Redis target on startup + an explicit ping so connection problems
are obvious in the deploy logs.
"""
from __future__ import annotations

from urllib.parse import urlparse

from redis import Redis
from rq import Queue, Worker

from app.config import settings
from app.workers.tasks import QUEUE_NAME


def _log_redis_target(url: str) -> None:
    u = urlparse(url or "")
    pw = u.password or ""
    masked = f"{pw[:3]}…{pw[-3:]}" if len(pw) >= 6 else "(empty/short)"
    print(
        f"[worker] REDIS target -> scheme={u.scheme} host={u.hostname} port={u.port} "
        f"user={u.username!r} password_len={len(pw)} password={masked}",
        flush=True,
    )


def main() -> None:
    _log_redis_target(settings.redis_url)
    connection = Redis.from_url(settings.redis_url)
    try:
        connection.ping()
        print("[worker] Redis ping OK — starting worker on queue 'indexing'", flush=True)
    except Exception as exc:  # noqa: BLE001 — surface the real reason in the deploy log
        print(f"[worker] Redis ping FAILED: {type(exc).__name__}: {exc}", flush=True)
        raise
    queue = Queue(QUEUE_NAME, connection=connection)
    Worker([queue], connection=connection).work()


if __name__ == "__main__":
    main()
