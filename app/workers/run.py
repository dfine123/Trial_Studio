"""Worker entrypoint:  python -m app.workers.run

Starts an RQ worker on the `indexing` queue using REDIS_URL from settings (so it works on
Railway without relying on the rq CLI's localhost default or shell var-expansion).
"""
from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from app.config import settings
from app.workers.tasks import QUEUE_NAME


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    queue = Queue(QUEUE_NAME, connection=connection)
    Worker([queue], connection=connection).work()


if __name__ == "__main__":
    main()
