"""FastAPI app + routes (Phase 0): health, upload, and read endpoints."""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from app import schemas
from app.db import SessionLocal, engine
from app.models import Audio, Clip, User
from app.storage import r2
from app.workers.tasks import enqueue_index
from app.corpus import grades as grade_store

_DEFAULT_NICHE = (
    "very-online absurdist humor — deadpan superiority + relatable jokes, a little mean; "
    "luxury / status is the visual backdrop, not the topic. range across everyday life: "
    "people, habits, social circles, dating, self-improvement clichés. money only as spice."
)
_REELS_DIR = "var/reels"
_WEB_DIR = os.path.join(os.path.dirname(__file__), "static")


class GenerateRequest(BaseModel):
    audio_id: uuid.UUID | None = None
    notes: str | None = None


class CapGenRequest(BaseModel):
    notes: str | None = None
    n: int = 8


class GradeRequest(BaseModel):
    caption: str
    verdict: str  # "keep" | "kill"
    note: str | None = None
    context: dict | None = None


class PairRequest(BaseModel):
    winner: str
    loser: str
    context: dict | None = None


class BestRequest(BaseModel):
    winner: str
    batch: list[str]
    context: dict | None = None


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


# ── Web app (Phase 2) ─────────────────────────────────────────
@app.get("/")
def home():
    return FileResponse(os.path.join(_WEB_DIR, "index.html"))


@app.get("/api/audios")
def api_audios():
    with SessionLocal() as s:
        rows = s.scalars(select(Audio).order_by(Audio.created_at)).all()
        return [
            {
                "id": str(a.id),
                "description": a.description or os.path.basename(a.r2_key or "audio"),
                "bpm": a.bpm or 0.0,
                "duration": a.duration or 0.0,
            }
            for a in rows
        ]


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    """One-button reel generation: audio -> caption -> beat-cut selection -> render."""
    with SessionLocal() as s:
        audio = s.get(Audio, req.audio_id) if req.audio_id else s.scalar(select(Audio).order_by(func.random()).limit(1))
    if audio is None or not audio.r2_key:
        raise HTTPException(status_code=404, detail="no audio in library — run the seed")

    audio_path = os.path.join("samples", "audio", os.path.basename(audio.r2_key))
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail=f"audio file missing locally: {audio_path}")

    os.makedirs(_REELS_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}.mp4"
    out = os.path.join(_REELS_DIR, name)
    niche = (req.notes or "").strip()  # only the user's optional nudge; engine voice = the corpus

    from app.generate.generator import generate_reel  # lazy: keeps web import light

    try:
        res = generate_reel(audio_path=audio_path, niche=niche, out_path=out,
                            audio_desc=audio.description, audio_bpm=audio.bpm,
                            audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"generation failed: {exc}") from exc

    return {
        "reel_url": f"/reels/{name}",
        "caption": res["caption"],
        "duration": res["duration"],
        "shots": res["shots"],
    }


@app.api_route("/reels/{name}", methods=["GET", "HEAD"])
def get_reel(name: str):
    path = os.path.join(_REELS_DIR, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="reel not found")
    return FileResponse(path, media_type="video/mp4")


# ── Caption grading (reward-model data capture) ───────────────
@app.get("/grade")
def grade_page():
    return FileResponse(os.path.join(_WEB_DIR, "grade.html"))


@app.post("/api/captions/generate")
def api_captions_generate(req: CapGenRequest):
    from app.caption.engine import generate  # lazy import (pulls anthropic + corpus)

    try:
        cands = generate(notes=req.notes, n=req.n)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"generation failed: {exc}") from exc
    return {"candidates": cands}


@app.post("/api/captions/grade")
def api_captions_grade(req: GradeRequest):
    grade_store.record_verdict(req.caption, req.verdict, req.context, req.note)
    return {"ok": True}


@app.post("/api/captions/pairwise")
def api_captions_pairwise(req: PairRequest):
    grade_store.record_pairwise(req.winner, req.loser, req.context)
    return {"ok": True}


@app.post("/api/captions/best")
def api_captions_best(req: BestRequest):
    grade_store.record_best(req.winner, req.batch, req.context)
    return {"ok": True}


@app.get("/api/captions/stats")
def api_captions_stats():
    g = grade_store.load_grades()
    verdicts = [x for x in g if x.get("type") == "verdict"]
    return {
        "total": len(g),
        "keeps": sum(1 for x in verdicts if x.get("verdict") == "keep"),
        "kills": sum(1 for x in verdicts if x.get("verdict") == "kill"),
        "best": sum(1 for x in g if x.get("type") == "best"),
    }
