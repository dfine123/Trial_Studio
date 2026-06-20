"""FastAPI app + routes (Phase 0): health, upload, and read endpoints."""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app import schemas
from app.db import SessionLocal, engine
from app.models import Clip, User
from app.storage import r2
from app.workers.tasks import enqueue_index


def ensure_default_user() -> uuid.UUID:
    """Seed one default user (no auth in V1; everything keys off user_id)."""
    with SessionLocal() as s:
        u = s.scalar(select(User).order_by(User.created_at).limit(1))
        if u is None:
            u = User(handle="default", description="default V1 user")
            s.add(u)
            s.commit()
            s.refresh(u)
        return u.id


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app.state.default_user_id = ensure_default_user()
    except Exception:  # noqa: BLE001 — don't block startup if DB isn't ready yet
        app.state.default_user_id = None
    yield


app = FastAPI(title="Trial Studio — Indexing", version="0.0.1", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    db_ok, detail = False, None
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
    return {"status": "ok" if db_ok else "degraded", "service": "trial-studio-indexing",
            "database": db_ok, "detail": detail}


@app.post("/clips", response_model=schemas.ClipCreated, status_code=201)
async def create_clip(file: UploadFile = File(...)):
    """Accept a video, store to R2 (user-scoped), enqueue async indexing, return the clip id."""
    user_id = ensure_default_user()
    clip_id = uuid.uuid4()
    ext = os.path.splitext(file.filename or "")[1].lower() or ".mp4"
    key = f"users/{user_id}/clips/{clip_id}/source{ext}"

    try:
        r2.upload_fileobj(key, file.file, content_type=file.content_type or "video/mp4")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"R2 upload failed: {exc}") from exc

    with SessionLocal() as s:
        s.add(Clip(id=clip_id, user_id=user_id, r2_key=key, status="uploaded"))
        s.commit()

    enqueue_index(clip_id)
    return schemas.ClipCreated(id=clip_id, status="uploaded")


@app.get("/clips", response_model=list[schemas.ClipListItem])
def list_clips():
    with SessionLocal() as s:
        rows = s.scalars(select(Clip).order_by(Clip.created_at.desc())).all()
        return [schemas.ClipListItem.model_validate(r) for r in rows]


@app.get("/clips/{clip_id}", response_model=schemas.ClipDetail)
def get_clip(clip_id: uuid.UUID):
    with SessionLocal() as s:
        clip = s.scalar(
            select(Clip).options(selectinload(Clip.segments)).where(Clip.id == clip_id)
        )
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        return schemas.ClipDetail.model_validate(clip)
