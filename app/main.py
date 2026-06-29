"""FastAPI app + routes (Phase 0): health, upload, and read endpoints."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from app import profiles, schemas
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Audio, Clip, ClipFolder, Template, User
from app.templates.spec import TemplateSpec
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
# Serialize clip indexing (OpenCV is memory-heavy) so a batch upload can't OOM-crash the instance.
_INDEX_SEM = threading.Semaphore(1)
_DEBUG_JOBS: dict = {}  # last /api/debug/generate-start job, polled by /api/debug/generate-result


class GenerateRequest(BaseModel):
    audio_id: uuid.UUID | None = None
    notes: str | None = None
    folder_id: uuid.UUID | None = None   # restrict generation to this folder (+ its sub-folders)
    no_caption: bool = False             # blank-caption reel: beat-cut clips + audio, no text overlay


class TemplateCreate(BaseModel):
    name: str
    audio_id: uuid.UUID | None = None
    spec: dict


class TemplateUpdate(BaseModel):
    name: str | None = None
    audio_id: uuid.UUID | None = None
    spec: dict | None = None


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


class ValidateRequest(BaseModel):
    name: str                 # reel filename (from reel_url)
    caption: str | None = None


class FolderCreate(BaseModel):
    name: str
    parent_id: uuid.UUID | None = None


class ClipMove(BaseModel):
    folder_id: uuid.UUID | None = None


class ProfileCreate(BaseModel):
    name: str
    niche: str | None = None


class ProfileActivate(BaseModel):
    id: uuid.UUID


class PersonaUpdate(BaseModel):
    persona: str


def ensure_default_user() -> uuid.UUID:
    """The default profile id (first user = the 'Spence' profile, voice seeded). Used for SHARED
    writes (audio/template); clip/folder writes use the ACTIVE profile instead."""
    return profiles.ensure_default_profile()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # NOTE: table creation happens in start.sh (before uvicorn) for fresh DBs (Railway); locally
    # the tables already exist, so we don't touch the schema here (it can stall startup).
    try:
        app.state.default_user_id = ensure_default_user()
    except Exception:  # noqa: BLE001 — don't block startup if DB isn't ready yet
        app.state.default_user_id = None
    yield


app = FastAPI(title="Trial Studio — Indexing", version="0.0.1", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=_WEB_DIR), name="assets")


# ── treelz.ai auth (local single-user demo gate) ──────────────
class LoginRequest(BaseModel):
    username: str
    password: str


def _auth_token(user: str) -> str:
    sig = hmac.new(settings.treelz_secret.encode(), user.encode(), hashlib.sha256).hexdigest()
    return f"{user}.{sig}"


def _is_authed(request: Request) -> bool:
    tok = request.cookies.get("treelz_session") or ""
    if "." not in tok:
        return False
    user, sig = tok.rsplit(".", 1)
    expected = hmac.new(settings.treelz_secret.encode(), user.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.post("/api/login")
def api_login(req: LoginRequest, response: Response):
    if req.username == settings.treelz_user and req.password == settings.treelz_password:
        response.set_cookie("treelz_session", _auth_token(req.username),
                            httponly=True, max_age=2592000, samesite="lax")
        return {"ok": True}
    raise HTTPException(status_code=401, detail="wrong username or password")


@app.post("/api/logout")
def api_logout(response: Response):
    response.delete_cookie("treelz_session")
    return {"ok": True}


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
    user_id = profiles.active_id()
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
        rows = s.scalars(select(Clip).where(Clip.user_id == profiles.active_id())
                         .order_by(Clip.created_at.desc())).all()
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


# ── Clip library + upload (treelz.ai) ─────────────────────────
@app.get("/api/clips/library")
def api_clips_library(folder_id: str | None = None):
    """Clips with tags + status, newest first. folder_id filters the view: a folder id shows clips
    DIRECTLY in it, 'none' shows unfiled clips, omitted shows all."""
    with SessionLocal() as s:
        q = (select(Clip).options(selectinload(Clip.segments))
             .where(Clip.user_id == profiles.active_id()).order_by(Clip.created_at.desc()))
        if folder_id == "none":
            q = q.where(Clip.folder_id.is_(None))
        elif folder_id:
            q = q.where(Clip.folder_id == uuid.UUID(folder_id))
        rows = s.scalars(q).all()
        return [
            {
                "id": str(c.id),
                "status": c.status,
                "duration": round(c.duration, 1) if c.duration else None,
                "summary": c.summary,
                "vibe_tags": c.vibe_tags or [],
                "setting": c.setting,
                "time_of_day": c.time_of_day,
                "camera_movement": c.camera_movement,
                "rejection_reason": c.rejection_reason,
                "segments": len(c.segments),
                "filename": os.path.basename(c.r2_key) if c.r2_key else None,
                "folder_id": str(c.folder_id) if c.folder_id else None,
            }
            for c in rows
        ]


@app.post("/api/clips/upload")
async def api_clips_upload(file: UploadFile = File(...), folder_id: str | None = Form(None)):
    """Upload a clip -> save locally -> index in a background thread (run_pipeline). The UI polls
    /api/clips/{id}/status to drive the indexing progress bar. Optionally filed into a folder."""
    user_id = profiles.active_id()
    clip_id = uuid.uuid4()
    ext = os.path.splitext(file.filename or "")[1].lower() or ".mp4"
    os.makedirs("var/uploads", exist_ok=True)
    dest = os.path.abspath(os.path.join("var/uploads", f"{clip_id}{ext}"))
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    fid = uuid.UUID(folder_id) if folder_id else None
    with SessionLocal() as s:
        s.add(Clip(id=clip_id, user_id=user_id, r2_key=dest, status="uploaded", folder_id=fid))
        s.commit()

    def _index() -> None:
        print(f"[idx-thread] {clip_id} thread running, importing pipeline…", flush=True)
        try:
            from app.indexing.pipeline import run_pipeline  # heavy (cv2 + TL) — import in the thread
            print(f"[idx-thread] {clip_id} pipeline imported, acquiring slot…", flush=True)
            with _INDEX_SEM:  # one clip indexes at a time — bounds memory so a batch can't OOM
                print(f"[idx-thread] {clip_id} slot acquired, indexing…", flush=True)
                run_pipeline(clip_id, source_path=dest)
        except Exception as exc:  # noqa: BLE001 — record the failure on the clip, never crash
            import traceback
            print(f"[idx-thread] {clip_id} EXCEPTION: {exc}", flush=True)
            traceback.print_exc()
            with SessionLocal() as s:
                c = s.get(Clip, clip_id)
                if c is not None and c.status != "indexed":
                    c.status = "rejected"
                    c.rejection_reason = str(exc)[:300]
                    s.commit()

    threading.Thread(target=_index, daemon=True).start()
    return {"clip_id": str(clip_id), "status": "uploaded", "filename": file.filename}


@app.get("/api/clips/{clip_id}/status")
def api_clip_status(clip_id: uuid.UUID):
    with SessionLocal() as s:
        c = s.get(Clip, clip_id)
        if c is None:
            raise HTTPException(status_code=404, detail="clip not found")
        return {
            "id": str(c.id), "status": c.status, "vibe_tags": c.vibe_tags or [],
            "setting": c.setting, "time_of_day": c.time_of_day, "summary": c.summary,
            "duration": round(c.duration, 1) if c.duration else None,
            "rejection_reason": c.rejection_reason,
            "folder_id": str(c.folder_id) if c.folder_id else None,
        }


# ── Folders (treelz.ai) ───────────────────────────────────────
def _folder_subtree_ids(folder_id: uuid.UUID) -> set:
    """folder_id + all descendant folder ids — so generating from a folder includes its sub-folders."""
    with SessionLocal() as s:
        rows = s.scalars(select(ClipFolder).where(ClipFolder.user_id == profiles.active_id())).all()
    children: dict = {}
    for f in rows:
        children.setdefault(f.parent_id, []).append(f.id)
    out, stack = set(), [folder_id]
    while stack:
        fid = stack.pop()
        if fid in out:
            continue
        out.add(fid)
        stack.extend(children.get(fid, []))
    return out


def _clip_ids_in_folder(folder_id: uuid.UUID) -> list[str]:
    """Indexed clip ids in a folder + all its sub-folders (for folder-scoped generation)."""
    ids = _folder_subtree_ids(folder_id)
    with SessionLocal() as s:
        clips = s.scalars(select(Clip).where(
            Clip.folder_id.in_(ids), Clip.user_id == profiles.active_id(), Clip.status == "indexed")).all()
    return [str(c.id) for c in clips]


@app.get("/api/folders")
def api_folders():
    """All folders (flat: id, name, parent_id, direct clip count). The UI builds the tree."""
    act = profiles.active_id()
    with SessionLocal() as s:
        folders = s.scalars(select(ClipFolder).where(ClipFolder.user_id == act)
                            .order_by(ClipFolder.name)).all()
        counts: dict = {}
        for (fid,) in s.execute(select(Clip.folder_id)
                                .where(Clip.folder_id.isnot(None), Clip.user_id == act)).all():
            counts[fid] = counts.get(fid, 0) + 1
        return [
            {"id": str(f.id), "name": f.name,
             "parent_id": str(f.parent_id) if f.parent_id else None,
             "clips": counts.get(f.id, 0)}
            for f in folders
        ]


@app.post("/api/folders")
def api_create_folder(req: FolderCreate):
    with SessionLocal() as s:
        f = ClipFolder(user_id=profiles.active_id(),
                       name=(req.name or "Untitled").strip()[:255] or "Untitled",
                       parent_id=req.parent_id)
        s.add(f)
        s.commit()
        s.refresh(f)
        return {"id": str(f.id), "name": f.name,
                "parent_id": str(f.parent_id) if f.parent_id else None, "clips": 0}


@app.delete("/api/folders/{folder_id}")
def api_delete_folder(folder_id: uuid.UUID):
    """Delete a folder: sub-folders cascade (FK), clips in it become unfiled (folder_id -> NULL)."""
    with SessionLocal() as s:
        f = s.get(ClipFolder, folder_id)
        if f is not None:
            s.delete(f)
            s.commit()
    return {"ok": True}


# ── Profiles (the platform's core unit: each profile = a creator with its OWN clips/folders/voice;
#    templates + the audio library are shared) ──────────────────
@app.get("/api/profiles")
def api_profiles():
    return profiles.list_profiles()


@app.post("/api/profiles")
def api_profile_create(req: ProfileCreate):
    return profiles.create_profile(req.name, req.niche)


@app.post("/api/profiles/active")
def api_profile_activate(req: ProfileActivate):
    with SessionLocal() as s:
        if s.get(User, req.id) is None:
            raise HTTPException(status_code=404, detail="profile not found")
    profiles.set_active(req.id)
    return {"ok": True, "active": str(req.id)}


@app.delete("/api/profiles/{profile_id}")
def api_profile_delete(profile_id: uuid.UUID):
    try:
        profiles.delete_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/profiles/{profile_id}/persona")
def api_profile_persona_get(profile_id: uuid.UUID):
    """The profile's authored VOICE persona (the per-profile half; the format base is shared in code)."""
    return {"persona": profiles.read_persona(profile_id)}


@app.post("/api/profiles/{profile_id}/persona")
def api_profile_persona_set(profile_id: uuid.UUID, req: PersonaUpdate):
    with SessionLocal() as s:
        if s.get(User, profile_id) is None:
            raise HTTPException(status_code=404, detail="profile not found")
    profiles.write_persona(profile_id, req.persona)
    return {"ok": True}


@app.post("/api/clips/{clip_id}/move")
def api_move_clip(clip_id: uuid.UUID, req: ClipMove):
    with SessionLocal() as s:
        c = s.get(Clip, clip_id)
        if c is None:
            raise HTTPException(status_code=404, detail="clip not found")
        c.folder_id = req.folder_id
        s.commit()
    return {"ok": True}


@app.delete("/api/clips/{clip_id}")
def api_delete_clip(clip_id: uuid.UUID):
    """Delete a clip + its segments (cascade), best-effort removing the uploaded file + thumb."""
    with SessionLocal() as s:
        c = s.get(Clip, clip_id)
        if c is not None:
            path = c.r2_key
            s.delete(c)
            s.commit()
            # remove our uploaded video (absolute paths only, never sample basenames) + the cached thumb
            stale = [os.path.join("var", "thumbs", f"{clip_id}.jpg")]
            if path and os.path.isabs(path):
                stale.append(path)
            for p in stale:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    return {"ok": True}


@app.get("/api/clips/{clip_id}/thumb")
def api_clip_thumb(clip_id: uuid.UUID):
    """Lazy poster-frame thumbnail — ffmpeg extracts one frame the first time, cached on the volume."""
    import subprocess

    thumb_dir = os.path.join("var", "thumbs")
    thumb_path = os.path.join(thumb_dir, f"{clip_id}.jpg")
    if not os.path.exists(thumb_path):
        os.makedirs(thumb_dir, exist_ok=True)
        with SessionLocal() as s:
            c = s.get(Clip, clip_id)
            src = c.r2_key if c else None
        if not src or not os.path.exists(src):
            raise HTTPException(status_code=404, detail="no source video")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "0.3", "-i", src, "-frames:v", "1",
                 "-vf", "scale=360:-2", "-q:v", "4", thumb_path],
                check=True, capture_output=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="thumbnail unavailable") from exc
    return FileResponse(thumb_path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/debug/index-test")
def api_debug_index_test(clip_id: str | None = None):
    """Diagnostic: run run_pipeline DIRECTLY on one clip (latest stuck clip if none given), in a
    watchdog thread, and return the step trace + status/exception/hung. Visit in a browser to see
    exactly where indexing stalls on THIS host — no log-digging. Bypasses the upload's bg thread,
    so a success here vs. a stuck upload points at the threading; a hang/error points at the pipeline."""
    import threading
    import traceback as _tb
    from app.indexing import pipeline as _pl

    with SessionLocal() as s:
        if clip_id:
            clip = s.get(Clip, uuid.UUID(clip_id))
        else:
            clip = s.scalar(
                select(Clip)
                .where(Clip.user_id == profiles.active_id())
                .where(Clip.status.in_(["uploaded", "indexing", "rejected"]))
                .order_by(Clip.created_at.desc())
            )
        if clip is None:
            return {"error": "no uploaded/indexing/rejected clip to test — upload one first"}
        cid, src = str(clip.id), clip.r2_key

    start = len(_pl.INDEX_TRACE)
    out: dict = {"clip_id": cid, "source": src, "source_exists": bool(src and os.path.exists(src))}

    def _run() -> None:
        try:
            out["status"] = _pl.run_pipeline(cid, source_path=src)
        except Exception as exc:  # noqa: BLE001
            out["exception"] = repr(exc)
            out["traceback"] = _tb.format_exc().splitlines()[-25:]

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=120)
    out["hung_after_120s"] = t.is_alive()
    out["trace"] = _pl.INDEX_TRACE[start:][-50:]
    return out


@app.get("/api/debug/generate-test")
def api_debug_generate_test():
    """Diagnostic: check generation prerequisites + run generate_reel once, returning the exact
    error/traceback. Visit in a browser; paste the JSON. Never exposes key values, only set/unset."""
    import threading
    import traceback as _tb

    out: dict = {}
    with SessionLocal() as s:
        _pid = profiles.active_id()
        out["clips_total"] = s.scalar(select(func.count()).select_from(Clip).where(Clip.user_id == _pid))
        out["clips_indexed"] = s.scalar(select(func.count()).select_from(Clip)
                                        .where(Clip.status == "indexed", Clip.user_id == _pid))
        sample = s.scalar(select(Clip).where(Clip.status == "indexed", Clip.user_id == _pid).limit(1))
        out["sample_clip_source"] = sample.r2_key if sample else None
        out["sample_source_exists"] = bool(sample and sample.r2_key and os.path.exists(sample.r2_key))
        audio = s.scalar(select(Audio).order_by(func.random()).limit(1))
    if audio is None:
        out["error"] = "no audio seeded"
        return out

    audio_path = os.path.join("samples", "audio", os.path.basename(audio.r2_key)) if audio.r2_key else ""
    out["audio"] = audio.description
    out["audio_exists"] = bool(audio_path and os.path.exists(audio_path))
    out["caption_provider"] = settings.caption_provider
    out["anthropic_key_set"] = bool(settings.anthropic_api_key)
    out["openai_key_set"] = bool(settings.openai_api_key)

    os.makedirs(_REELS_DIR, exist_ok=True)
    out_path = os.path.join(_REELS_DIR, f"debugtest_{uuid.uuid4().hex}.mp4")
    box: dict = {}

    def _run() -> None:
        from app.generate.generator import generate_reel
        try:
            r = generate_reel(audio_path=audio_path, niche="", out_path=out_path,
                              audio_desc=audio.description, audio_bpm=audio.bpm,
                              audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags)
            box["status"] = "ok"
            box["result"] = list(r.keys()) if isinstance(r, dict) else str(r)[:200]
        except Exception as exc:  # noqa: BLE001
            box["exception"] = repr(exc)
            box["traceback"] = _tb.format_exc().splitlines()[-30:]

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=220)
    out["hung_after_220s"] = t.is_alive()
    out.update(box)
    return out


@app.get("/api/debug/generate-start")
def api_debug_generate_start():
    """Kick off ONE generate_reel in the BACKGROUND and return instantly, so Railway's edge can't
    time out the long request (that timeout is the likely bug). Then poll /api/debug/generate-result."""
    import threading
    import time
    import traceback as _tb

    with SessionLocal() as s:
        _pid = profiles.active_id()
        indexed = s.scalar(select(func.count()).select_from(Clip)
                           .where(Clip.status == "indexed", Clip.user_id == _pid))
        audio = s.scalar(select(Audio).order_by(func.random()).limit(1))
        sample = s.scalar(select(Clip).where(Clip.status == "indexed", Clip.user_id == _pid).limit(1))
    if audio is None:
        return {"error": "no audio seeded"}
    audio_path = os.path.join("samples", "audio", os.path.basename(audio.r2_key)) if audio.r2_key else ""
    job: dict = {
        "state": "running",
        "clips_indexed": indexed,
        "audio": audio.description,
        "audio_exists": bool(audio_path and os.path.exists(audio_path)),
        "sample_source_exists": bool(sample and sample.r2_key and os.path.exists(sample.r2_key)),
        "caption_provider": settings.caption_provider,
        "anthropic_key_set": bool(settings.anthropic_api_key),
        "openai_key_set": bool(settings.openai_api_key),
    }
    _DEBUG_JOBS["last"] = job
    os.makedirs(_REELS_DIR, exist_ok=True)
    out_path = os.path.join(_REELS_DIR, f"dbg_{uuid.uuid4().hex}.mp4")

    def _run() -> None:
        from app.generate.generator import generate_reel
        t0 = time.monotonic()
        try:
            r = generate_reel(audio_path=audio_path, niche="", out_path=out_path,
                              audio_desc=audio.description, audio_bpm=audio.bpm,
                              audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags)
            job["state"] = "done"
            job["result"] = list(r.keys()) if isinstance(r, dict) else str(r)[:200]
        except Exception as exc:  # noqa: BLE001
            job["state"] = "error"
            job["exception"] = repr(exc)
            job["traceback"] = _tb.format_exc().splitlines()[-30:]
        job["seconds"] = round(time.monotonic() - t0, 1)

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, **job}


@app.get("/api/debug/generate-result")
def api_debug_generate_result():
    """Poll the last /api/debug/generate-start job: state running|done|error, + result/exception/seconds."""
    return _DEBUG_JOBS.get("last", {"state": "none — hit /api/debug/generate-start first"})


# ── treelz.ai web app ─────────────────────────────────────────
@app.get("/login")
def login_page():
    return FileResponse(os.path.join(_WEB_DIR, "login.html"))


@app.get("/templates")
def templates_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "templates.html"))


@app.get("/")
def home(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "app.html"))


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

    audio_path = _audio_path(audio)
    if audio_path is None:
        raise HTTPException(status_code=404, detail="audio file missing")

    os.makedirs(_REELS_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}.mp4"
    out = os.path.join(_REELS_DIR, name)
    niche = (req.notes or "").strip()  # only the user's optional nudge; engine voice = the corpus

    from app.generate.generator import generate_reel  # lazy: keeps web import light

    clip_ids = None
    if req.folder_id:
        clip_ids = _clip_ids_in_folder(req.folder_id)
        if not clip_ids:
            raise HTTPException(status_code=400, detail="no indexed clips in that folder yet — upload + index some first")

    try:
        res = generate_reel(audio_path=audio_path, niche=niche, out_path=out,
                            audio_desc=audio.description, audio_bpm=audio.bpm,
                            audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags,
                            clip_ids=clip_ids, no_caption=req.no_caption)
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


# ── Template Studio ───────────────────────────────────────────
def _audio_path(a) -> str | None:
    """Resolve an Audio's file: an uploaded absolute path (var/uploads/audio), else samples/audio."""
    if not a or not a.r2_key:
        return None
    if os.path.isabs(a.r2_key) and os.path.exists(a.r2_key):
        return a.r2_key
    p = os.path.join("samples", "audio", os.path.basename(a.r2_key))
    return p if os.path.exists(p) else None


@app.get("/api/audio/{audio_id}/file")
def api_audio_file(audio_id: uuid.UUID):
    """Serve the raw audio file so the studio timeline can play + scrub it."""
    with SessionLocal() as s:
        a = s.get(Audio, audio_id)
    path = _audio_path(a)
    if path is None:
        raise HTTPException(status_code=404, detail="audio file missing")
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/audios/upload")
async def api_audio_upload(file: UploadFile = File(...), description: str | None = Form(None)):
    """Upload an audio -> analyze its beat grid (librosa) -> store. Powers the template-studio timeline."""
    os.makedirs("var/uploads/audio", exist_ok=True)
    aid = uuid.uuid4()
    ext = os.path.splitext(file.filename or "")[1].lower() or ".mp3"
    dest = os.path.abspath(os.path.join("var/uploads/audio", f"{aid}{ext}"))
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    bpm = duration = None
    beat_map: list = []
    try:
        from app.audio import profile  # lazy: librosa is heavy
        bp = profile.analyze(dest)
        bpm, duration, beat_map = bp.bpm, bp.duration, bp.beat_map
    except Exception:  # noqa: BLE001 — keep the audio even if beat analysis fails (no grid -> free placement)
        pass
    desc = (description or "").strip() or os.path.splitext(file.filename or "audio")[0]
    with SessionLocal() as s:
        a = Audio(id=aid, user_id=ensure_default_user(), r2_key=dest, source="upload",
                  description=desc[:255], bpm=bpm, duration=duration, beat_map=beat_map)
        s.add(a)
        s.commit()
    return {"id": str(aid), "description": desc, "bpm": bpm or 0.0,
            "duration": duration or 0.0, "beats": len(beat_map or [])}


@app.get("/api/audio/{audio_id}/beats")
def api_audio_beats(audio_id: uuid.UUID):
    """Beat grid + bpm + energy for the template-studio timeline (snap segment marks to beats)."""
    with SessionLocal() as s:
        a = s.get(Audio, audio_id)
        if a is None:
            raise HTTPException(status_code=404, detail="audio not found")
        return {
            "id": str(a.id), "description": a.description,
            "bpm": a.bpm or 0.0, "duration": a.duration or 0.0,
            "beat_map": a.beat_map or [], "energy_arc": a.energy_arc,
            "beat_drop_ts": a.beat_drop_ts, "file_url": f"/api/audio/{a.id}/file",
        }


@app.get("/api/templates")
def api_templates():
    with SessionLocal() as s:
        rows = s.scalars(select(Template).order_by(Template.created_at.desc())).all()
        return [
            {"id": str(t.id), "name": t.name, "audio_id": str(t.audio_id) if t.audio_id else None,
             "segments": len((t.spec or {}).get("segments", [])),
             "created_at": t.created_at.isoformat() if t.created_at else None}
            for t in rows
        ]


@app.get("/api/templates/{template_id}")
def api_template_get(template_id: uuid.UUID):
    with SessionLocal() as s:
        t = s.get(Template, template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        return {"id": str(t.id), "name": t.name,
                "audio_id": str(t.audio_id) if t.audio_id else None, "spec": t.spec}


@app.post("/api/templates")
def api_template_create(req: TemplateCreate):
    """Persist a template authored in the studio. Validates the free-form dual-record spec."""
    try:
        spec = TemplateSpec.model_validate(req.spec)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid template spec: {exc}") from exc
    with SessionLocal() as s:
        t = Template(user_id=ensure_default_user(), name=(req.name or "Untitled").strip()[:255] or "Untitled",
                     audio_id=req.audio_id, spec=spec.model_dump())
        s.add(t)
        s.commit()
        s.refresh(t)
        return {"id": str(t.id), "name": t.name}


@app.put("/api/templates/{template_id}")
def api_template_update(template_id: uuid.UUID, req: TemplateUpdate):
    """Update a template — used to re-link an audio (or rename / replace the spec)."""
    from sqlalchemy.orm.attributes import flag_modified
    with SessionLocal() as s:
        t = s.get(Template, template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        if req.name is not None:
            t.name = (req.name.strip()[:255] or t.name)
        if req.audio_id is not None:
            t.audio_id = req.audio_id
        if req.spec is not None:
            try:
                t.spec = TemplateSpec.model_validate(req.spec).model_dump()
                flag_modified(t, "spec")
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid template spec: {exc}") from exc
        s.commit()
    return {"ok": True}


@app.delete("/api/templates/{template_id}")
def api_template_delete(template_id: uuid.UUID):
    with SessionLocal() as s:
        t = s.get(Template, template_id)
        if t is not None:
            s.delete(t)
            s.commit()
    return {"ok": True}


@app.post("/api/templates/{template_id}/enrich")
def api_template_enrich(template_id: uuid.UUID):
    """Run the LLM interpreter on a template -> a variability-aware Formula Object (per-slot: what's
    locked vs variable + under what conditions). Persist it on spec.formula and return it."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.templates.interpret import interpret_template
    with SessionLocal() as s:
        t = s.get(Template, template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        spec = dict(t.spec or {})
        fo = interpret_template(spec)
        if not fo:
            raise HTTPException(status_code=502, detail="interpretation failed — try again")
        prev = spec.get("formula") or {}
        fo.setdefault("exemplar_arc", prev.get("exemplar_arc", []))
        spec["formula"] = fo
        t.spec = spec
        flag_modified(t, "spec")
        s.commit()
    return {"formula": fo}


@app.post("/api/templates/{template_id}/instantiate")
def api_template_instantiate(template_id: uuid.UUID):
    """Apply a template to the creator's clips -> render a multi-segment reel (match -> regenerate
    captions under the variability rules -> compose). Aborts with a clear message if unfillable."""
    with SessionLocal() as s:
        t = s.get(Template, template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        spec = dict(t.spec or {})
        audio = s.get(Audio, t.audio_id) if t.audio_id else None
    audio_path = _audio_path(audio)
    if audio_path is None:
        raise HTTPException(status_code=400, detail="template has no usable audio — re-pick one in the studio")
    os.makedirs(_REELS_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}.mp4"
    out = os.path.join(_REELS_DIR, name)
    from app.templates.instantiate import instantiate_template
    try:
        res = instantiate_template(spec, audio_path, out)
    except Exception as exc:  # noqa: BLE001 — the abort/usability message is user-facing
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"reel_url": f"/reels/{name}", "captions": res["captions"],
            "segments": res["segments"], "duration": res["duration"]}


def _slug(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:maxlen].strip("-") or "reel"


@app.post("/api/reels/validate")
def api_reels_validate(req: ValidateRequest):
    """Mark a reel postable -> copy it (+ a caption sidecar .txt) into the export folder (your Google
    Drive for Desktop synced folder). Named by a caption slug so the folder is scannable; idempotent;
    logged to var/validated.jsonl."""
    src = os.path.join(_REELS_DIR, os.path.basename(req.name))
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="reel not found (already cleaned up?)")
    if os.path.getsize(src) < 100_000:  # a real reel is multiple MB; a broken/partial render is bytes
        raise HTTPException(status_code=422, detail=f"reel looks broken/empty ({os.path.getsize(src)} bytes) — regenerate before validating")
    export_dir = settings.reel_export_dir
    try:
        os.makedirs(export_dir, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"export folder not writable: {export_dir} ({exc})")
    stem = _slug(req.caption) + "__" + os.path.splitext(os.path.basename(req.name))[0][:8]
    dest_mp4 = os.path.join(export_dir, stem + ".mp4")
    try:
        if not os.path.exists(dest_mp4):
            shutil.copy2(src, dest_mp4)
        if (req.caption or "").strip():
            with open(os.path.join(export_dir, stem + ".txt"), "w", encoding="utf-8") as f:
                f.write(req.caption.strip() + "\n")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"export copy failed: {exc}")
    os.makedirs("var", exist_ok=True)
    with open("var/validated.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"name": req.name, "caption": req.caption, "exported": dest_mp4}, ensure_ascii=False) + "\n")
    return {"ok": True, "exported": dest_mp4}


# ── Caption grading (reward-model data capture) ───────────────
@app.get("/grade")
def grade_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
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
