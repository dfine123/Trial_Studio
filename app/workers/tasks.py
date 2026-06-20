"""RQ task + queue helpers. Run a worker with:  rq worker indexing"""
from __future__ import annotations

from redis import Redis
from rq import Queue

from app.config import settings

QUEUE_NAME = "indexing"


def get_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=get_connection())


def index_clip(clip_id: str):
    """Worker entrypoint. Imported lazily so the web process stays light (no cv2/TL import)."""
    from app.indexing.pipeline import run_pipeline

    return run_pipeline(clip_id)


def enqueue_index(clip_id) -> str:
    job = get_queue().enqueue(index_clip, str(clip_id), job_timeout=3600)
    return job.id
