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
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import selectinload

from app import profiles, schemas
from app.config import settings
from app.db import SessionLocal, engine
from app import models
from app.models import Audio, Clip, ClipFolder, Template, User
from app.templates.spec import TemplateSpec
from app.storage import r2
from app.workers.tasks import enqueue_index
from app.corpus import attribute
from app.corpus import grades as grade_store

_DEFAULT_NICHE = (
    "very-online absurdist humor — deadpan superiority + relatable jokes, a little mean; "
    "luxury / status is the visual backdrop, not the topic. range across everyday life: "
    "people, habits, social circles, dating, self-improvement clichés. money only as spice."
)
_REELS_DIR = "var/reels"
_WEB_DIR = os.path.join(os.path.dirname(__file__), "static")
# Clips in flight (INDEX_CONCURRENCY, default 6): the long TwelveLabs remote waits overlap, while the
# memory-heavy OpenCV stages stay one-at-a-time via pipeline._CV2 (a batch still can't OOM the instance).
# TL side measured 2026-07-02: 8 simultaneous tasks accepted instantly, no 429s, parallel processing.
_INDEX_SEM = threading.Semaphore(max(1, settings.index_concurrency))
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


class BootstrapRequest(BaseModel):
    from_profile: uuid.UUID | None = None   # source voice to reskin (default: the Spence profile)
    limit: int = 200                        # default covers the FULL corpus — don't silently drop proven formats
    reset: bool = False                     # drop the previous bootstrap seed before re-seeding
    verbatim: bool = False                  # same-archetype seed: copy original refs as-is (no reskin)


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
    try:    # permanently drop any retired references from every profile's live corpus (self-healing)
        from app.corpus import retire
        retire.purge_all()
    except Exception:  # noqa: BLE001 — cleanup must never block boot
        pass
    try:    # a deploy/restart kills any in-flight Drive sync — release stale claims so re-sync works
        with SessionLocal() as s:
            s.execute(update(models.DriveConnection)
                      .where(models.DriveConnection.status == "syncing")
                      .values(status="connected"))
            s.commit()
    except Exception:  # noqa: BLE001 — table may not exist yet on first boot
        pass
    if settings.demo_mode:
        try:    # demo service: seed the Base voice + audio library (idempotent)
            from app import demo
            demo.boot()
        except Exception as exc:  # noqa: BLE001 — boot must not block; /health still comes up
            print(f"[demo] boot seed failed: {exc}", flush=True)
    try:    # Telegram reference-intake bot (operator-only; no-op unless env creds are set)
        from app.reference.telegram import start_bot_if_configured
        if start_bot_if_configured():
            print("[tg] reference bot started", flush=True)
    except Exception as exc:  # noqa: BLE001 — the bot must never block boot
        print(f"[tg] bot start failed: {exc}", flush=True)
    yield


app = FastAPI(title="Trial Studio — Indexing", version="0.0.1", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=_WEB_DIR), name="assets")


# ── DEMO MODE gate: path WHITELIST + per-request user scoping (dormant unless DEMO_MODE=1) ──
@app.middleware("http")
async def demo_gate(request: Request, call_next):
    if not settings.demo_mode:
        return await call_next(request)
    from fastapi.responses import JSONResponse
    from app import demo
    allowed, needs_auth = demo.route_allowed(request.method, request.url.path)
    if not allowed:
        return JSONResponse({"detail": "not found"}, status_code=404)
    uid = demo.session_uid(request)
    if needs_auth and uid is None and not _is_authed(request):   # the OPERATOR may pass too (admin)
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    token = profiles.set_request_uid(uid)
    try:
        return await call_next(request)
    finally:
        profiles.reset_request_uid(token)


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
            "database": db_ok, "detail": detail,
            "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]}


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
    if settings.demo_mode:
        from app import demo
        if demo.clips_used(profiles.active_id()) >= settings.demo_max_clips:
            raise HTTPException(status_code=400,
                                detail=f"library full — the demo caps at {settings.demo_max_clips} clips "
                                       "(delete a clip to make room)")
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


def _demo_owns_clip(c) -> None:
    """DEMO: a session may only touch its own clips (prod operator is unaffected)."""
    if settings.demo_mode and c is not None and c.user_id != profiles.active_id():
        raise HTTPException(status_code=404, detail="clip not found")


@app.get("/api/clips/{clip_id}/status")
def api_clip_status(clip_id: uuid.UUID):
    with SessionLocal() as s:
        c = s.get(Clip, clip_id)
        if c is None:
            raise HTTPException(status_code=404, detail="clip not found")
        _demo_owns_clip(c)
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


# ── VOICES: any profile can generate with any profile's voice (persisted per profile) ──────────
@app.get("/api/voices")
def api_voices():
    """Selectable voices (profiles that have a corpus), with the ACTIVE profile's current pick."""
    from app.corpus.store import load_refs
    act = profiles.active_id()
    cur = profiles.voice_id()
    out = []
    with SessionLocal() as s:
        rows = s.scalars(select(User).order_by(User.created_at)).all()
    for u in rows:
        refs = len(load_refs(profiles.corpus_path(u.id)))
        if refs == 0 and u.id != cur:
            continue   # a profile with no voice yet isn't a usable voice
        out.append({"profile_id": str(u.id), "label": (u.voice_label or u.handle or "Untitled"),
                    "refs": refs, "active": u.id == cur})
    return {"voices": out, "for_profile": str(act)}


class VoiceSelect(BaseModel):
    voice_profile_id: uuid.UUID


@app.post("/api/voice")
def api_voice_select(req: VoiceSelect):
    """Point the ACTIVE profile at a voice. Generation, rotation, and learning all follow it."""
    with SessionLocal() as s:
        if s.get(User, req.voice_profile_id) is None:
            raise HTTPException(status_code=404, detail="voice profile not found")
    profiles.set_voice(profiles.active_id(), req.voice_profile_id)
    return {"ok": True, "voice_profile_id": str(req.voice_profile_id)}


class VoiceLabelReq(BaseModel):
    label: str


@app.post("/api/profiles/{profile_id}/voice-label")
def api_voice_label(profile_id: uuid.UUID, req: VoiceLabelReq):
    """Rename how this profile's VOICE displays in the picker (e.g. Austin's voice -> 'Base')."""
    with SessionLocal() as s:
        u = s.get(User, profile_id)
        if u is None:
            raise HTTPException(status_code=404, detail="profile not found")
        u.voice_label = (req.label or "").strip()[:64] or None
        s.commit()
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/bootstrap-voice")
def api_bootstrap_voice(profile_id: uuid.UUID, req: BootstrapRequest):
    """Cold-start a profile's voice corpus by reskinning a SOURCE profile's proven formats into this
    profile's voice (per its persona). Append-only. Then it can generate + be graded into its own voice."""
    with SessionLocal() as s:
        if s.get(User, profile_id) is None:
            raise HTTPException(status_code=404, detail="profile not found")
    src = req.from_profile or profiles.ensure_default_profile()
    from app.caption.bootstrap import bootstrap_from
    try:
        n = bootstrap_from(target=profile_id, source=src, limit=req.limit, reset=req.reset,
                           verbatim=req.verbatim)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"bootstrap failed: {exc}") from exc
    return {"ok": True, "added": n}


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
        _demo_owns_clip(c)
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

    if settings.demo_mode:
        with SessionLocal() as s:
            _demo_owns_clip(s.get(Clip, clip_id))
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
    if settings.demo_mode:
        return FileResponse(os.path.join(_WEB_DIR, "demo.html"))   # handles its own auth screen
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "app.html"))


# ── DEMO accounts + status (all dormant unless DEMO_MODE=1; the middleware whitelists them) ──
class DemoAuthRequest(BaseModel):
    username: str
    password: str


def _demo_only():
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="not found")


@app.post("/api/demo/signup")
def api_demo_signup(req: DemoAuthRequest, response: Response):
    _demo_only()
    from app import demo
    uid = demo.signup(req.username, req.password)
    response.set_cookie("demo_session", demo.mint_session(uid), httponly=True,
                        max_age=2592000, samesite="lax")
    return {"ok": True, "username": req.username.strip().lower()}


@app.post("/api/demo/login")
def api_demo_login(req: DemoAuthRequest, response: Response):
    _demo_only()
    from app import demo
    uid = demo.login(req.username, req.password)
    response.set_cookie("demo_session", demo.mint_session(uid), httponly=True,
                        max_age=2592000, samesite="lax")
    return {"ok": True, "username": req.username.strip().lower()}


@app.post("/api/demo/logout")
def api_demo_logout(response: Response):
    _demo_only()
    response.delete_cookie("demo_session")
    return {"ok": True}


@app.get("/api/demo/me")
def api_demo_me():
    _demo_only()
    uid = profiles.active_id()
    with SessionLocal() as s:
        u = s.get(models.User, uid)
    return {"username": (u.handle if u else None), "user_id": str(uid)}


@app.get("/api/demo/status")
def api_demo_status():
    _demo_only()
    from app import demo
    uid = profiles.active_id()
    with SessionLocal() as s:
        indexed = s.scalar(select(func.count()).select_from(Clip)
                           .where(Clip.user_id == uid, Clip.status == "indexed")) or 0
    st = demo.quota_state(uid)
    st.update({"clips_used": demo.clips_used(uid), "clips_max": settings.demo_max_clips,
               "clips_indexed": indexed, "clip_seconds_max": settings.demo_max_clip_seconds})
    return st


# ── DEMO ADMIN (operator-only: env-cred cookie via /api/login; demo sessions can't pass) ──
@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(_WEB_DIR, "admin.html"))   # the page gates itself via the API


def _operator_only(request: Request) -> None:
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="operator only")


@app.get("/api/admin/overview")
def api_admin_overview(request: Request):
    """Accounts + activity: every demo signup (password_hash marks demo accounts) with clip counts,
    reel usage, quota state; aggregate stat cards on top."""
    _operator_only(request)
    import time as _time
    from app import demo
    from app.corpus import reels as reel_store
    now = _time.time()
    accounts, tot_clips, tot_indexed, tot_rejected, tot_reels, cooling = [], 0, 0, 0, 0, 0
    with SessionLocal() as s:
        users = s.scalars(select(models.User).where(models.User.password_hash.isnot(None))
                          .order_by(models.User.created_at.desc())).all()
        for u in users:
            counts = dict(s.execute(select(Clip.status, func.count()).where(Clip.user_id == u.id)
                                    .group_by(Clip.status)).all())
            n_all = sum(counts.values())
            n_ok = counts.get("indexed", 0)
            n_rej = counts.get("rejected", 0)
            recs = reel_store._load(u.id)
            q = demo.quota_state(u.id)
            last_reel = None
            for r in recs[::-1]:
                p = os.path.join(_REELS_DIR, os.path.basename(r.get("reel_url") or ""))
                if os.path.exists(p):
                    last_reel = os.path.getmtime(p)
                    break
            if q["cooldown_until"]:
                cooling += 1
            tot_clips += n_all; tot_indexed += n_ok; tot_rejected += n_rej; tot_reels += len(recs)
            accounts.append({
                "username": u.handle, "created_at": u.created_at.isoformat() if u.created_at else None,
                "clips_total": n_all, "clips_indexed": n_ok, "clips_rejected": n_rej,
                "clips_busy": n_all - n_ok - n_rej,
                "reels_total": len(recs), "reels_window": q["reels_used"], "reels_max": q["reels_max"],
                "cooldown_seconds": q["resets_in_seconds"],
                "last_reel_ago": int(now - last_reel) if last_reel else None,
            })
    reels_24h = 0
    try:
        reels_24h = sum(1 for f in os.listdir(_REELS_DIR)
                        if f.endswith(".mp4") and now - os.path.getmtime(os.path.join(_REELS_DIR, f)) < 86400)
    except OSError:
        pass
    return {"totals": {"accounts": len(accounts), "clips": tot_clips, "clips_indexed": tot_indexed,
                       "clips_rejected": tot_rejected, "reels": tot_reels, "reels_24h": reels_24h,
                       "in_cooldown": cooling},
            "accounts": accounts}


@app.get("/api/admin/reels")
def api_admin_reels(request: Request, limit: int = 30):
    """The live feed: latest reels across every demo account (caption + who made it + playable url)."""
    _operator_only(request)
    import time as _time
    from app.corpus import reels as reel_store
    now = _time.time()
    out = []
    with SessionLocal() as s:
        users = s.scalars(select(models.User).where(models.User.password_hash.isnot(None))).all()
        for u in users:
            for r in reel_store._load(u.id):
                p = os.path.join(_REELS_DIR, os.path.basename(r.get("reel_url") or ""))
                if not os.path.exists(p):
                    continue
                out.append({"username": u.handle, "caption": r.get("caption"),
                            "reel_url": r.get("reel_url"), "ago": int(now - os.path.getmtime(p))})
    out.sort(key=lambda x: x["ago"])
    return {"reels": out[:max(1, min(100, limit))]}


@app.get("/api/demo/reels")
def api_demo_reels():
    _demo_only()
    from app.corpus import reels as reel_store
    uid = profiles.active_id()
    rows = reel_store._load(uid)
    return {"reels": [{"reel_url": r.get("reel_url"), "caption": r.get("caption")}
                      for r in reversed(rows) if r.get("reel_url")]}


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
def api_generate(req: GenerateRequest, backend: str | None = None):
    """One-button reel generation: audio -> caption -> beat-cut selection -> render.

    `backend` (TEST only): 'sonnet' | 'openai' routes the whole pipeline to that model + an isolated
    reels/taste/rotation store; None = production Opus (unchanged)."""
    if settings.demo_mode:
        from app import demo
        demo.check_quota(profiles.active_id())    # 429 with Retry-After during the cooldown
        backend = None                            # demo never routes to test models
        req.no_caption = False                    # the caption IS the demo
        req.audio_id = None                       # audio is auto-matched to the caption
        req.folder_id = None
    from app.caption import backend as _bk
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

    # A TEST backend (Sonnet/OpenAI) routes the whole pipeline to that model + writes to an ISOLATED
    # reels/rotation store; None = production Opus. Wraps caption gen + reel build + the grading record.
    with _bk.using(backend):
        # AUDIO-FIRST MATCH: when the operator lets us pick the track ("Mix"), generate the caption FIRST and
        # pick the audio whose vibe amplifies it — instead of the blind random draw above.
        pre_caption, pre_cands = None, None
        if req.audio_id is None and not req.no_caption:
            try:
                from app.generate.generator import generate_caption, match_audio
                with SessionLocal() as s:
                    choices = list(s.scalars(select(Audio).where(Audio.r2_key.isnot(None))).all())
                if len(choices) > 1:
                    pre_caption, pre_cands = generate_caption(niche or None)
                    bi = match_audio(pre_caption, [f"{a.description or ''} (energy: {a.energy_arc or '?'})" for a in choices])
                    audio = choices[bi]
                    _p = _audio_path(audio)
                    if _p:
                        audio_path = _p
            except Exception:  # noqa: BLE001 — fall back to the random audio + inline caption gen
                pre_caption, pre_cands = None, None

        try:
            res = generate_reel(audio_path=audio_path, niche=niche, out_path=out,
                                audio_desc=audio.description, audio_bpm=audio.bpm,
                                audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags,
                                clip_ids=clip_ids, no_caption=req.no_caption,
                                caption_text=pre_caption, caption_candidates=pre_cands)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"generation failed: {exc}") from exc

        try:    # capture the production context for END-OUTPUT grading (chosen caption + candidates + clips + audio)
            from app.corpus import reels
            reels.append({
                "reel_id": name.rsplit(".", 1)[0],
                "reel_url": f"/reels/{name}",
                "audio": {"id": str(audio.id), "description": audio.description},
                "caption": res.get("caption"),
                "caption_id": res.get("caption_id"),
                "caption_anchor_refs": res.get("caption_anchor_refs") or [],
                "candidates": res.get("candidates") or [],
                "clips": res.get("clips") or [],
                "folder_id": str(req.folder_id) if req.folder_id else None,   # recaption keeps clip scope
                "voice_profile_id": str(profiles.voice_id()),   # which VOICE generated it (grades credit it)
            })
        except Exception:   # noqa: BLE001 — recording must never break generation
            pass

    if settings.demo_mode:
        from app import demo
        demo.count_reel(profiles.active_id())     # only a SUCCESSFUL reel consumes quota

    return {
        "reel_url": f"/reels/{name}",
        "reel_id": name.rsplit(".", 1)[0],
        "caption": res["caption"],
        "options": [{"text": c.get("text"), "chosen": bool(c.get("chosen"))}
                    for c in (res.get("candidates") or []) if (c.get("text") or "").strip()],
        "duration": res["duration"],
        "shots": res["shots"],
    }


# ── Batch generation: captions SERIAL (anti-repeat window + rotation state see each slate
# before the next starts — parallel captions are how duplicate premises happen), renders
# (clip-match + ffmpeg, the ~2/3 of wall-clock) PARALLEL in a bounded pool. ~2.5-3x faster
# batches with zero caption-integrity risk. ──
_BATCH_JOBS: dict[str, dict] = {}
_BATCH_LOCK = __import__("threading").Lock()


class BatchGenerateRequest(BaseModel):
    n: int = 1
    audio_id: uuid.UUID | None = None
    notes: str | None = None
    folder_id: uuid.UUID | None = None
    no_caption: bool = False


def _run_batch(job_id: str, req: "BatchGenerateRequest") -> None:
    from concurrent.futures import ThreadPoolExecutor
    from app.corpus import reels as reel_store
    from app.generate.generator import generate_caption, generate_reel, match_audio
    import random as _random
    job = _BATCH_JOBS[job_id]
    niche = (req.notes or "").strip()
    batch_clip_used: dict[str, int] = {}   # shared clip ledger — parallel renders can't see each
                                           # other through clip_usage.json (same snapshot); this can
    used_audio: set[str] = set()           # batch AUDIO variety: caption-matching converges on one
                                           # "best" track for a same-toned batch (4/6 identical,
                                           # operator-flagged) — match among tracks unused this batch

    def fail(i: int, msg: str) -> None:
        with _BATCH_LOCK:
            job["reels"][i] = {"state": "failed", "error": msg[:200]}

    def render_one(i: int, audio_row: dict, audio_path: str, pre_caption, pre_cands, clip_ids) -> None:
        name = f"{uuid.uuid4().hex}.mp4"
        out = os.path.join(_REELS_DIR, name)
        try:
            res = generate_reel(audio_path=audio_path, niche=niche, out_path=out,
                                audio_desc=audio_row["description"], audio_bpm=audio_row["bpm"],
                                audio_energy=audio_row["energy_arc"], audio_vibe=audio_row["thematic_tags"],
                                clip_ids=clip_ids, no_caption=req.no_caption,
                                caption_text=pre_caption, caption_candidates=pre_cands,
                                batch_clip_used=batch_clip_used)
            try:
                reel_store.append({
                    "reel_id": name.rsplit(".", 1)[0],
                    "reel_url": f"/reels/{name}",
                    "audio": {"id": audio_row["id"], "description": audio_row["description"]},
                    "caption": res.get("caption"),
                    "caption_id": res.get("caption_id"),
                    "caption_anchor_refs": res.get("caption_anchor_refs") or [],
                    "candidates": res.get("candidates") or [],
                    "clips": res.get("clips") or [],
                    "folder_id": str(req.folder_id) if req.folder_id else None,
                    "voice_profile_id": str(profiles.voice_id()),
                })
            except Exception:  # noqa: BLE001 — recording must never break generation
                pass
            with _BATCH_LOCK:
                job["reels"][i] = {"state": "done", "reel_url": f"/reels/{name}",
                                   "reel_id": name.rsplit(".", 1)[0],
                                   "caption": res["caption"],
                                   "options": [{"text": c.get("text"), "chosen": bool(c.get("chosen"))}
                                               for c in (res.get("candidates") or [])
                                               if (c.get("text") or "").strip()],
                                   "duration": res["duration"],
                                   "shots": res["shots"], "audio_desc": audio_row["description"]}
        except Exception as exc:  # noqa: BLE001
            fail(i, f"render: {exc}")

    render_pool = ThreadPoolExecutor(max_workers=max(1, settings.reel_render_concurrency))
    futures = []
    try:
        clip_ids = None
        if req.folder_id:
            clip_ids = _clip_ids_in_folder(req.folder_id)
            if not clip_ids:
                for i in range(req.n):
                    fail(i, "no indexed clips in that folder")
                return
        for i in range(req.n):
            with _BATCH_LOCK:
                job["reels"][i]["state"] = "captioning"
            with SessionLocal() as s:
                audio = s.get(Audio, req.audio_id) if req.audio_id else \
                    s.scalar(select(Audio).order_by(func.random()).limit(1))
                choices = list(s.scalars(select(Audio).where(Audio.r2_key.isnot(None))).all())
            audio_pool = choices
            if req.audio_id is None:
                audio_pool = [a for a in choices if str(a.id) not in used_audio] or choices
                if audio is not None and str(audio.id) in used_audio and audio_pool:
                    audio = _random.choice(audio_pool)   # the random fallback also prefers unused tracks
            if audio is None or not audio.r2_key:
                fail(i, "no audio in library")
                continue
            audio_path = _audio_path(audio)
            if audio_path is None:
                fail(i, "audio file missing")
                continue
            pre_caption = pre_cands = None
            if not req.no_caption:
                try:   # caption FIRST (serial); audio matched to it when the operator didn't pin one
                    pre_caption, pre_cands = generate_caption(niche or None)
                    if req.audio_id is None and len(audio_pool) > 1:
                        bi = match_audio(pre_caption, [f"{a.description or ''} (energy: {a.energy_arc or '?'})"
                                                       for a in audio_pool])
                        audio = audio_pool[bi]
                        audio_path = _audio_path(audio) or audio_path
                except Exception:  # noqa: BLE001 — generate_reel falls back to inline caption gen
                    pre_caption = pre_cands = None
            if req.audio_id is None and audio is not None:
                used_audio.add(str(audio.id))
            audio_row = {"id": str(audio.id), "description": audio.description, "bpm": audio.bpm,
                         "energy_arc": audio.energy_arc, "thematic_tags": audio.thematic_tags}
            with _BATCH_LOCK:
                job["reels"][i]["state"] = "rendering"
            futures.append(render_pool.submit(render_one, i, audio_row, audio_path,
                                              pre_caption, pre_cands, clip_ids))
        for f in futures:
            f.result()
    except Exception as exc:  # noqa: BLE001 — a broken orchestrator must still close the job
        with _BATCH_LOCK:
            for i, r in enumerate(job["reels"]):
                if r.get("state") not in ("done", "failed"):
                    job["reels"][i] = {"state": "failed", "error": str(exc)[:200]}
    finally:
        try:    # nothing in cleanup may prevent the job from closing (a shadowed name here once
                # left jobs "running" forever)
            render_pool.shutdown(wait=True)
        except Exception:  # noqa: BLE001
            pass
        with _BATCH_LOCK:
            job["state"] = "done"


@app.post("/api/generate/batch")
def api_generate_batch(req: BatchGenerateRequest):
    """Start a reel batch as a background job; poll GET /api/generate/batch/{job_id}."""
    import threading
    if settings.demo_mode:
        raise HTTPException(status_code=403, detail="batch generation is operator-only")
    n = max(1, min(10, req.n))
    req.n = n
    job_id = uuid.uuid4().hex
    with _BATCH_LOCK:
        _BATCH_JOBS[job_id] = {"state": "running", "n": n,
                               "reels": [{"state": "queued"} for _ in range(n)]}
        while len(_BATCH_JOBS) > 20:   # keep the registry small
            _BATCH_JOBS.pop(next(iter(_BATCH_JOBS)))
    threading.Thread(target=_run_batch, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "n": n}


@app.get("/api/generate/batch/{job_id}")
def api_generate_batch_status(job_id: str):
    job = _BATCH_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown batch job")
    with _BATCH_LOCK:
        return json.loads(json.dumps(job))   # snapshot, not the live dict


# ── Recaption: the operator picked a DIFFERENT caption option on a reel card. Full caption-first
# re-production with that caption FIXED (clips re-react to the new line, duration re-scales; same
# audio track — it's part of the card's identity). The swap is logged on the record: "picked X
# over the default Y" is the highest-fidelity taste signal the system gets, straight from the
# operator's hands (no LLM judge involved). Runs as a background job (edge 502s long requests). ──
_RECAP_JOBS: dict[str, dict] = {}


class RecaptionRequest(BaseModel):
    reel_id: str
    caption: str


def _run_recaption(job_id: str, reel_id: str, caption: str) -> None:
    from app.corpus import reels as reel_store
    from app.generate.generator import generate_reel
    job = _RECAP_JOBS[job_id]
    try:
        rec = reel_store.get(reel_id)
        if rec is None:
            raise RuntimeError("unknown reel")
        audio_id = (rec.get("audio") or {}).get("id")
        with SessionLocal() as s:
            audio = s.get(Audio, uuid.UUID(audio_id)) if audio_id else None
        if audio is None or not audio.r2_key:
            raise RuntimeError("this reel's audio is no longer in the library")
        audio_path = _audio_path(audio)
        if audio_path is None:
            raise RuntimeError("audio file missing")
        clip_ids = None
        if rec.get("folder_id"):   # keep the original generation's clip scope
            clip_ids = _clip_ids_in_folder(uuid.UUID(rec["folder_id"])) or None
        cands = [dict(c) for c in (rec.get("candidates") or [])]
        for c in cands:   # the operator's pick becomes the chosen candidate (caption_id provenance)
            c["chosen"] = (c.get("text") or "").strip() == caption.strip()
        name = f"{uuid.uuid4().hex}.mp4"
        out = os.path.join(_REELS_DIR, name)
        res = generate_reel(audio_path=audio_path, niche="", out_path=out,
                            audio_desc=audio.description, audio_bpm=audio.bpm,
                            audio_energy=audio.energy_arc, audio_vibe=audio.thematic_tags,
                            clip_ids=clip_ids,
                            caption_text=caption, caption_candidates=cands,
                            work_png=f"tmp/recap_{job_id}.png")
        old_name = os.path.basename(rec.get("reel_url") or "")
        reel_store.record_recaption(reel_id, f"/reels/{name}", caption, res.get("clips") or [])
        if old_name and old_name != name:   # the superseded video: best-effort cleanup
            try:
                os.remove(os.path.join(_REELS_DIR, old_name))
            except OSError:
                pass
        job.update({"state": "done", "reel_url": f"/reels/{name}", "caption": caption,
                    "duration": res.get("duration"), "shots": res.get("shots")})
    except Exception as exc:  # noqa: BLE001
        job.update({"state": "failed", "error": str(exc)[:200]})


@app.post("/api/reels/recaption")
def api_reels_recaption(req: RecaptionRequest):
    """Re-produce a reel with an operator-picked caption option; poll GET /api/reels/recaption/{job_id}."""
    import threading
    if settings.demo_mode:
        raise HTTPException(status_code=403, detail="recaption is operator-only")
    caption = (req.caption or "").strip()
    if not caption:
        raise HTTPException(status_code=400, detail="empty caption")
    from app.corpus import reels as reel_store
    if reel_store.get(req.reel_id) is None:
        raise HTTPException(status_code=404, detail="unknown reel")
    job_id = uuid.uuid4().hex
    _RECAP_JOBS[job_id] = {"state": "running"}
    while len(_RECAP_JOBS) > 20:
        _RECAP_JOBS.pop(next(iter(_RECAP_JOBS)))
    threading.Thread(target=_run_recaption, args=(job_id, req.reel_id, caption), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/reels/recaption/{job_id}")
def api_reels_recaption_status(job_id: str):
    job = _RECAP_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown recaption job")
    return json.loads(json.dumps(job))


@app.api_route("/reels/{name}", methods=["GET", "HEAD"])
def get_reel(name: str, request: Request):
    safe = os.path.basename(name)
    if settings.demo_mode and not _is_authed(request):   # demo sessions fetch ONLY their own reels; the operator sees all
        from app.corpus import reels as reel_store
        mine = {os.path.basename(r.get("reel_url") or "") for r in reel_store._load(profiles.active_id())}
        if safe not in mine:
            raise HTTPException(status_code=404, detail="reel not found")
    path = os.path.join(_REELS_DIR, safe)
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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"export copy failed: {exc}")
    # Real Drive upload (OAuth as the operator): the reel lands in "treelz exports/<profile>" in THEIR
    # My Drive. Best-effort — a Drive hiccup or missing creds never blocks the local validate.
    drive = None
    try:
        from app.drive import gdrive as _gd
        from app.drive.export import upload_validated
        if _gd.export_configured():
            drive = upload_validated(profiles.active_id(), src, stem, req.caption)
    except Exception as exc:  # noqa: BLE001
        drive = {"error": str(exc)[:200]}
    os.makedirs("var", exist_ok=True)
    with open("var/validated.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"name": req.name, "caption": req.caption, "exported": dest_mp4,
                            "drive": drive}, ensure_ascii=False) + "\n")
    return {"ok": True, "exported": dest_mp4, "drive": drive}


# ── Caption grading (reward-model data capture) ───────────────
@app.get("/grade")
def grade_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "grade.html"))


# ── Google Drive sync (share a folder with the service account -> clips auto-ingest) ──
class DriveConnectReq(BaseModel):
    folder: str   # a Drive folder link or id, shared with the service account as Viewer


@app.post("/api/drive/connect")
def api_drive_connect(req: DriveConnectReq):
    """Connect a shared Drive folder to the ACTIVE profile (verifies the SA can actually see it)."""
    from app.drive import sync as drive_sync
    res = drive_sync.connect(profiles.active_id(), req.folder)
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error") or "could not access that folder")
    return {**res, "service_account": settings.google_sa_email}


@app.get("/api/drive/status")
def api_drive_status():
    """The active profile's Drive connections + counts + the share-with email."""
    from app.drive import sync as drive_sync
    return drive_sync.status(profiles.active_id())


@app.post("/api/drive/sync/{connection_id}")
def api_drive_sync(connection_id: uuid.UUID):
    """Kick a sync for one connection in the background (download -> index each new video)."""
    from app.drive import sync as drive_sync
    with SessionLocal() as s:
        conn = s.get(models.DriveConnection, connection_id)
        if conn is None or conn.user_id != profiles.active_id():
            raise HTTPException(status_code=404, detail="connection not found for this profile")
        if conn.status == "syncing":
            return {"ok": True, "already_syncing": True}
    threading.Thread(target=lambda: drive_sync.sync_connection(connection_id,
                                                               log=lambda m: print(m, flush=True)),
                     daemon=True).start()
    return {"ok": True, "syncing": True}


# ── Reel (end-output) grading: rate the finished reel + see the candidate captions it chose between ──
@app.get("/grade-reels")
def grade_reels_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "grade_reels.html"))


@app.get("/system")
def system_map_page(request: Request):
    """TEMPORARY (2026-07-15, operator request): a read-only visualization of the live caption
    pipeline — every prompt layer with live counts/texts, the guards, the chooser, the learn
    loop. Fetches operator-gated debug endpoints; changes nothing."""
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "system.html"))


@app.get("/promote")
def promote_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "promote.html"))


# ── THE LAB: isolated exploration lane — hot generation, 1-10 grading, >=8 auto-promotes into the voice ──
@app.get("/lab")
def lab_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(_WEB_DIR, "lab.html"))


class LabGenRequest(BaseModel):
    n: int = 8


class LabGradeRequest(BaseModel):
    caption_id: str
    rating: int


@app.post("/api/lab/generate")
def api_lab_generate(req: LabGenRequest):
    from app.caption import lab
    try:
        return {"candidates": lab.generate_lab(max(1, min(12, req.n)))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"lab generation failed: {exc}") from exc


@app.post("/api/lab/grade")
def api_lab_grade(req: LabGradeRequest):
    from app.caption import lab
    return lab.grade_lab(req.caption_id, req.rating)


@app.get("/api/lab/stats")
def api_lab_stats():
    from app.caption import lab
    return lab.lab_stats()


@app.post("/api/lab/rebuild-codex")
def api_lab_rebuild_codex():
    """Re-distill the lab's principles codex from the CURRENT evidence (refs + grades). Run after
    learn rounds so new promotions/notes feed the understanding."""
    from app.caption import lab
    try:
        return lab.build_codex(force=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"codex build failed: {exc}") from exc


@app.get("/api/reels/pending")
def api_reels_pending(backend: str | None = None):
    from app.caption import backend as _bk
    from app.corpus import reels
    with _bk.using(backend):
        return reels.pending()


@app.get("/api/reels/graded")
def api_reels_graded(backend: str | None = None):
    from app.caption import backend as _bk
    from app.corpus import reels
    with _bk.using(backend):
        return reels.graded()


@app.post("/api/reels/learn")
def api_reels_learn(backend: str | None = None):
    """Mine every graded reel's note (idempotent): pairwise 'you'd have posted X' corrections -> the
    chooser EVAL ground truth (/api/chooser/eval); off_voice negatives -> stored for voice review.
    (The distilled-taste refresh was removed from this flow — it narrowed selection when injected.)"""
    from app.caption import backend as _bk, taste
    from app.corpus import promote, reels
    with _bk.using(backend):
        pw, ov = 0, 0
        for r in reels.graded():
            try:
                got = taste.learn_from_reel(r)
                pw += 1 if got.get("pairwise") else 0
                ov += 1 if got.get("off_voice") else 0
            except Exception:  # noqa: BLE001
                pass
        # THE core-generator loop: every operator-validated line (posted >=8 + note-endorsed >=8) enters
        # the reference corpus — the generator's grounding grows from exactly what the operator rates best.
        grown = promote.promote_all()
        codex_ok = None
        try:    # v2 generation ideates FROM the codex — rebuild it so every learn round compounds
                # the understanding (new refs' decodes + the fresh notes) into the next generation
            from app.caption import lab
            codex_ok = bool(lab.build_codex(force=True).get("ok"))
        except Exception:  # noqa: BLE001 — codex refresh must never sink a learn run
            codex_ok = False
        return {"ok": True, "pairs_captured": pw, "off_voice_captured": ov,
                "codex_rebuilt": codex_ok,
                # 2026-07-15 realignment: THE SENSE (var/craft.md, /api/craft) is re-synthesized
                # BY THE AGENT after each graded round — from the round's notes + the corpus —
                # never mechanically (the brief-resynthesis precedent). This flag is the reminder.
                "sense_resynthesis_due": True, **grown}


@app.post("/api/reels/refresh-taste")
def api_reels_refresh_taste(backend: str | None = None):
    """(Re)distill the creator's taste from everything graded so far, cache it for the chooser."""
    from app.caption import backend as _bk, taste
    with _bk.using(backend):
        return taste.refresh_taste()


@app.get("/api/reels/calibration")
def api_reels_calibration(backend: str | None = None):
    """The distilled TASTE the chooser now reads — what makes this creator's captions hit (transparency)."""
    from app.caption import backend as _bk, taste
    with _bk.using(backend):
        return {"taste": taste.distilled_taste()}


@app.get("/api/refs/audit")
def api_refs_audit():
    """The ACTIVE VOICE's corpus size (what generation actually sees — follows the voice pointer) +
    any RETIRED reference still present (post-purge verification)."""
    from app import profiles
    from app.corpus import retire
    from app.corpus.store import load_refs
    pid = profiles.voice_id()
    return {"total_refs": len(load_refs(profiles.corpus_path(pid))),
            "voice_profile_id": str(pid),
            "retired_present": retire.retired_present(pid)}


@app.get("/api/debug/clip-sim")
def api_debug_clip_sim(ids: str | None = None, top: int = 15):
    """Embedding-similarity report for the ACTIVE profile's clips. Without `ids`: the pairwise cosine
    distribution + the most-similar pairs (calibrates CLIP_SIM_THRESHOLD — near-duplicate takes sit at
    the top). With `ids` (comma-separated): all pairwise sims among those clips (verify a reel's picks)."""
    import numpy as np
    with SessionLocal() as s:
        rows = s.execute(select(Clip.id, Clip.summary, Clip.embedding).where(
            Clip.user_id == profiles.active_id(), Clip.status == "indexed",
            Clip.embedding.isnot(None))).all()
    if ids:
        want = {x.strip() for x in ids.split(",") if x.strip()}
        rows = [r for r in rows if str(r[0]) in want]
    if len(rows) < 2:
        return {"clips_with_embeddings": len(rows), "pairs": []}
    E = np.array([r[2] for r in rows], dtype=float)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    S = E @ E.T
    iu = np.triu_indices(len(rows), k=1)
    sims = S[iu]
    order = np.argsort(-sims)[: max(1, top)]
    pairs = [{"a": str(rows[iu[0][k]][0]), "b": str(rows[iu[1][k]][0]), "sim": round(float(sims[k]), 4),
              "a_sum": (rows[iu[0][k]][1] or "")[:60], "b_sum": (rows[iu[1][k]][1] or "")[:60]}
             for k in order]
    return {"clips_with_embeddings": len(rows), "threshold": settings.clip_sim_threshold,
            "percentiles": {p: round(float(np.percentile(sims, p)), 4) for p in (50, 90, 95, 99)},
            "max": round(float(sims.max()), 4), "top_pairs": pairs}


@app.post("/api/debug/re-embed")
def api_debug_re_embed(dry: bool = False):
    """Repair corrupt clip embeddings for the ACTIVE profile: any embedding vector shared EXACTLY by
    2+ clips is garbage (unrelated footage can't be identical), so re-run the Marengo embed from each
    clip's source file and store the real vector. Serial + idempotent (healthy clips untouched).
    dry=true: no re-embedding — just report each corrupt clip's source-file state (diagnosis)."""
    import hashlib as _hashlib
    from app.indexing import twelvelabs as tl
    with SessionLocal() as s:
        rows = s.execute(select(Clip.id, Clip.r2_key, Clip.embedding, Clip.summary).where(
            Clip.user_id == profiles.active_id(), Clip.status == "indexed",
            Clip.embedding.isnot(None))).all()
    groups: dict = {}
    for cid, key, emb, summ in rows:
        h = _hashlib.sha1(json.dumps(emb).encode()).hexdigest()
        groups.setdefault(h, []).append((cid, key, summ))
    bad = [x for g in groups.values() if len(g) > 1 for x in g]
    if dry:
        return {"corrupt_clips": len(bad), "clips": [
            {"id": str(cid), "summary": (summ or "")[:50], "src": key,
             "src_exists": bool(key and os.path.exists(key)),
             "size": (os.path.getsize(key) if key and os.path.exists(key) else 0)}
            for cid, key, summ in bad]}
    bad = [(cid, key) for cid, key, _ in bad]
    if not bad:
        return {"corrupt_clips": 0, "re_embedded": 0, "failed": 0}
    c = tl.client()
    fixed, failed = 0, 0
    for cid, key in bad:
        try:
            if not key or not os.path.exists(key):
                failed += 1
                continue
            vec = tl.embed_video(c, video_file=key)
            if vec:
                with SessionLocal() as s:
                    cl = s.get(Clip, cid)
                    if cl is not None:
                        cl.embedding = vec
                        s.commit()
                fixed += 1
            else:
                failed += 1
        except Exception:  # noqa: BLE001 — one clip must not stop the repair
            failed += 1
    return {"corrupt_clips": len(bad), "re_embedded": fixed, "failed": failed}


@app.get("/api/debug/genlog-dump")
def api_genlog_dump(request: Request, n: int = 300):
    """Operator-only: the last n generated captions (the raw pool) for drift forensics."""
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="operator only")
    from app.corpus.genlog import recent_generated
    return {"captions": recent_generated(max(1, min(2000, n)))}


@app.get("/api/debug/lane-stats")
def api_lane_stats(request: Request):
    """Operator-only: per-engine grade ledger for the active voice (which lanes' defaults the
    operator has actually graded, and how they scored). Populated at grade time; empty until
    v3-era reels get graded."""
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="operator only")
    from collections import defaultdict
    rows = []
    try:
        with open(profiles.voice_file("lane_stats.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        pass
    agg: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        agg[r.get("engine") or "?"].append(int(r.get("rating") or 0))
    return {"observations": len(rows),
            "lanes": {e: {"n": len(v), "mean": round(sum(v) / len(v), 2),
                          "ge8": sum(1 for x in v if x >= 8), "le4": sum(1 for x in v if x <= 4)}
                      for e, v in sorted(agg.items())}}


@app.get("/api/debug/corpus-dump")
def api_corpus_dump(request: Request):
    """Operator-only: the ACTIVE VOICE's full corpus + persona (used to export the Base voice as
    the demo seed). Requires the operator session cookie."""
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="operator only")
    from app.corpus.store import load_refs
    pid = profiles.voice_id()
    return {"voice_profile_id": str(pid), "persona": profiles.read_persona(pid),
            "refs": load_refs(profiles.corpus_path(pid))}


@app.get("/api/debug/length-audit")
def api_debug_length_audit():
    """Caption-length forensics across the three layers (canon: measure corpus-vs-pool-vs-chosen
    BEFORE blaming a layer). corpus = the active voice's references (split originals/promoted);
    pool = every generated candidate (genlog, time-ordered thirds show drift); chooser = for each
    reel batch, the CHOSEN caption's length rank among its own candidates (0=shortest, 1=longest;
    a length-neutral chooser averages ~0.5)."""
    from app.corpus import reels as reel_store
    from app.corpus.store import load_refs

    def wc(t):
        return len((t or "").split())

    def stats(ws):
        ws = sorted(ws)
        n = len(ws)
        if not n:
            return {"n": 0}
        return {"n": n, "mean": round(sum(ws) / n, 1), "median": ws[n // 2],
                "p25": ws[n // 4], "p75": ws[(3 * n) // 4]}

    refs = load_refs(profiles.corpus_path())
    orig = [wc(r.get("caption")) for r in refs if not str(r.get("ref_id", "")).startswith("p")]
    promo = [wc(r.get("caption")) for r in refs if str(r.get("ref_id", "")).startswith("p")]

    gen_path = profiles.genlog_path()
    gen_rows = []
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            gen_rows = [json.loads(x) for x in f if x.strip()]
    gws = [wc(r.get("text")) for r in gen_rows]
    third = max(1, len(gws) // 3)

    ranks, chosen_ws, pool_ws, longest_hits, shortest_hits, reel_rows = [], [], [], 0, 0, []
    for rec in reel_store._load():
        cands = [c for c in (rec.get("candidates") or []) if (c.get("text") or "").strip()]
        ch = next((c for c in cands if c.get("chosen")), None)
        if not ch or len(cands) < 2:
            continue
        lens = [wc(c["text"]) for c in cands]
        cl = wc(ch["text"])
        below = sum(1 for x in lens if x < cl)
        ties = sum(1 for x in lens if x == cl) - 1
        rank = (below + 0.5 * ties) / (len(lens) - 1)
        ranks.append(rank)
        chosen_ws.append(cl)
        pool_ws.extend(lens)
        longest_hits += int(cl == max(lens))
        shortest_hits += int(cl == min(lens))
        reel_rows.append({"rank": round(rank, 2), "chosen_words": cl,
                          "batch": sorted(lens), "rated": bool(rec.get("grade"))})
    half = max(1, len(ranks) // 2)
    return {
        "corpus": {"originals": stats(orig), "promoted": stats(promo),
                   "all": stats(orig + promo)},
        "pool_genlog": {"all": stats(gws),
                        "first_third": stats(gws[:third]),
                        "last_third": stats(gws[-third:])},
        "chooser": {
            "reels_measured": len(ranks),
            "mean_length_rank": round(sum(ranks) / len(ranks), 3) if ranks else None,
            "picked_longest_pct": round(100 * longest_hits / len(ranks), 1) if ranks else None,
            "picked_shortest_pct": round(100 * shortest_hits / len(ranks), 1) if ranks else None,
            "older_half_rank": round(sum(ranks[:half]) / half, 3) if ranks else None,
            "recent_half_rank": round(sum(ranks[half:]) / max(1, len(ranks) - half), 3) if ranks else None,
            "chosen_words": stats(chosen_ws), "batch_words": stats(pool_ws),
            "recent_reels": reel_rows[-12:],
        },
    }


@app.get("/api/debug/clip-probe")
def api_debug_clip_probe(ids: str | None = None, reel_id: str | None = None):
    """Diagnose clips against their REAL files: DB duration vs container vs video-stream duration,
    how far segments reach, embedding state, and per-segment quality. Pass ?ids=a,b,c or
    ?reel_id=<reel> (probes the clips that reel used)."""
    from app.corpus import reels as reel_store
    from app.indexing.qc import ffprobe as _ffprobe
    from app.models import Segment
    want: list[str] = [x.strip() for x in (ids or "").split(",") if x.strip()]
    if reel_id:
        rec = next((r for r in reel_store._load() if r.get("reel_id") == reel_id), None)
        if rec is None:
            raise HTTPException(status_code=404, detail="reel not found")
        want += [c.get("clip_id") for c in (rec.get("clips") or []) if c.get("clip_id")]
    out = []
    with SessionLocal() as s:
        for cid in want:
            clip = s.get(Clip, uuid.UUID(cid))
            if clip is None:
                out.append({"clip_id": cid, "error": "not found"})
                continue
            segs = s.scalars(select(Segment).where(Segment.clip_id == clip.id).order_by(Segment.idx)).all()
            row = {
                "clip_id": cid, "summary": (clip.summary or "")[:70],
                "db_duration": clip.duration,
                "max_segment_end": max((sg.end_ts or 0.0) for sg in segs) if segs else None,
                "segments": [{"start": sg.start_ts, "end": sg.end_ts,
                              "usability": sg.usability_score, "luminance": sg.luminance} for sg in segs],
                "embedding": ("none" if not clip.embedding else
                              "constant" if len(set(clip.embedding[:64])) <= 1 else "ok"),
            }
            if clip.r2_key and os.path.exists(clip.r2_key):
                try:
                    import subprocess as _sp
                    p = _ffprobe(clip.r2_key)
                    fmt = _sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nokey=1:noprint_wrappers=1", clip.r2_key],
                                  capture_output=True, text=True)
                    row["video_stream_duration"] = round(p.duration, 3)
                    row["container_duration"] = round(float(fmt.stdout.strip() or 0.0), 3)
                    row["overrun"] = bool(row["max_segment_end"] and
                                          row["max_segment_end"] > p.duration + 0.05)
                except Exception as exc:  # noqa: BLE001
                    row["probe_error"] = str(exc)[:120]
            else:
                row["probe_error"] = "source file missing"
            out.append(row)
    return {"clips": out}


@app.post("/api/debug/repair-durations")
def api_debug_repair_durations(dry: bool = True):
    """Repair phantom footage across ALL indexed clips: re-probe each source's real VIDEO-STREAM
    duration (containers routinely outlive the last video frame); fix Clip.duration and clamp/drop
    segments that reach past it. Those phantom windows are what froze reels mid-play. Idempotent."""
    from app.indexing.qc import ffprobe as _ffprobe
    from app.models import Segment
    scanned = fixed_clips = trimmed = dropped = missing = 0
    changes = []
    with SessionLocal() as s:
        clips = s.scalars(select(Clip).where(Clip.status == "indexed")).all()
        for clip in clips:
            scanned += 1
            if not (clip.r2_key and os.path.exists(clip.r2_key)):
                missing += 1
                continue
            try:
                real = _ffprobe(clip.r2_key).duration
            except Exception:  # noqa: BLE001
                missing += 1
                continue
            if not real:
                missing += 1
                continue
            over = (clip.duration or 0.0) - real
            segs = s.scalars(select(Segment).where(Segment.clip_id == clip.id)).all()
            seg_over = [sg for sg in segs if (sg.end_ts or 0.0) > real + 0.05]
            if over <= 0.05 and not seg_over:
                continue
            change = {"clip_id": str(clip.id), "summary": (clip.summary or "")[:50],
                      "db_duration": clip.duration, "real_duration": round(real, 3),
                      "segments_trimmed": 0, "segments_dropped": 0}
            if not dry:
                clip.duration = round(real, 3)
            fixed_clips += 1
            for sg in seg_over:
                if (sg.start_ts or 0.0) >= real - 0.5:      # fully (or almost fully) phantom
                    change["segments_dropped"] += 1
                    dropped += 1
                    if not dry:
                        s.delete(sg)
                else:                                        # trim the phantom tail off
                    change["segments_trimmed"] += 1
                    trimmed += 1
                    if not dry:
                        sg.end_ts = round(real, 3)
                        sg.duration = round(real - (sg.start_ts or 0.0), 3)
            changes.append(change)
        if not dry:
            s.commit()
    return {"dry": dry, "scanned": scanned, "clips_fixed": fixed_clips,
            "segments_trimmed": trimmed, "segments_dropped": dropped,
            "source_missing": missing, "changes": changes}


class AuthoredPrune(BaseModel):
    contains: str = ""


@app.post("/api/debug/authored-prune")
def api_debug_authored_prune(req: AuthoredPrune):
    """Inspect / surgically remove 'authored' grade records (operator-written note captions). Empty
    `contains` = list only. With `contains` = remove matching records — for when the miner misfiles
    a payoff FRAGMENT as a standalone caption (it would re-promote on every learn otherwise)."""
    from app.corpus import grades as grade_store
    recs = grade_store.load_grades()
    authored = [r for r in recs if r.get("type") == "authored"]
    needle = (req.contains or "").strip().lower()
    if not needle:
        return {"authored": [{"caption": r.get("caption"), "claim": r.get("claim")} for r in authored]}
    kept = [r for r in recs if not (r.get("type") == "authored"
                                    and needle in (r.get("caption") or "").lower())]
    removed = len(recs) - len(kept)
    if removed:
        grade_store._rewrite(kept)
    return {"removed": removed,
            "authored_left": [{"caption": r.get("caption"), "claim": r.get("claim")}
                              for r in kept if r.get("type") == "authored"]}


@app.get("/api/debug/grades-dump")
def api_debug_grades_dump(type: str | None = None):
    """Read-only dump of the ACTIVE VOICE's grade records (pairwise/verdict/best/authored) — the
    chooser-eval ground truth lives here and was previously unreadable off-volume."""
    from app.corpus import grades as grade_store
    recs = grade_store.load_grades()
    if type:
        recs = [r for r in recs if r.get("type") == type]
    return {"n": len(recs), "records": recs}


class CorpusAdd(BaseModel):
    caption: str
    rating: int = 8
    note: str | None = None


@app.post("/api/debug/corpus-add")
def api_debug_corpus_add(req: CorpusAdd):
    """Directly add ONE operator-authored/validated caption to the active voice's corpus (the
    fallback when the note-miner misses a hand-written line — operator gold must never depend on
    extraction luck). Deduped + why_it_works-decoded like every promotion."""
    from app.corpus.promote import _add_ref
    rid = _add_ref(req.caption, req.rating, [], "operator_authored",
                   req.note or "operator-added directly", op_note=req.note)
    return {"ref_id": rid, "already": rid is None}


class ProfileSettings(BaseModel):
    max_shots: int | None = None          # 1-2 for single-clip profiles; None/0 = mashup (unbounded)
    reference_active: bool | None = None  # include this profile in Telegram reference recreations


@app.get("/api/profiles/{pid}/settings")
def api_profile_settings_get(pid: str):
    return profiles.profile_settings(uuid.UUID(pid))


@app.post("/api/profiles/{pid}/settings")
def api_profile_settings_set(pid: str, req: ProfileSettings):
    """PROFILE-owned style knobs (max_shots for 1-2 clip profiles; reference_active for the
    Telegram intake bot). Own-profile file — style follows the creator's footage, independent
    of the borrowed voice."""
    patch: dict = {}
    if req.max_shots is not None:
        patch["max_shots"] = int(req.max_shots) if req.max_shots > 0 else 0
    if req.reference_active is not None:
        patch["reference_active"] = bool(req.reference_active)
    return profiles.set_profile_settings(patch, uuid.UUID(pid))


class ReferenceIntake(BaseModel):
    url: str


@app.post("/api/debug/reference-intake")
def api_debug_reference_intake(req: ReferenceIntake):
    """Run the Telegram intake pipeline directly (testing/manual). Synchronous; long."""
    if settings.demo_mode:
        raise HTTPException(status_code=404)
    from app.reference.intake import find_reel_url, process_reel_link
    url = find_reel_url(req.url or "")
    if not url:
        raise HTTPException(status_code=400, detail="no instagram reel url found")
    lines: list[str] = []
    results = process_reel_link(url, notify=lines.append)
    return {"results": results, "log": lines}


class NorthStarAdd(BaseModel):
    caption: str
    point: str | None = None
    stance: str | None = None


@app.get("/api/northstars")
def api_northstars_list():
    from app.caption import northstars
    return {"north_stars": northstars.load()}


@app.post("/api/northstars")
def api_northstars_add(req: NorthStarAdd):
    """Operator intake for gold-standard captions from the wild — THE BAR generation writes to."""
    from app.caption import northstars
    return northstars.add(req.caption, req.point, req.stance)


@app.delete("/api/northstars/{ns_id}")
def api_northstars_remove(ns_id: str):
    from app.caption import northstars
    if not northstars.remove(ns_id):
        raise HTTPException(status_code=404, detail="unknown north star")
    return {"ok": True}


class VoiceCoreUpdate(BaseModel):
    text: str


@app.get("/api/voice-core")
def api_voice_core_get():
    from app.caption.engine import voice_core
    return {"text": voice_core()}


@app.post("/api/voice-core")
def api_voice_core_set(req: VoiceCoreUpdate):
    """The OPERATOR edits the system's taste directly — this text sits in every v2 generation."""
    t = (req.text or "").strip()
    if len(t) < 100:
        raise HTTPException(status_code=400, detail="core text suspiciously short — refusing")
    path = os.path.join("var", "voice_core.md")
    os.makedirs("var", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, path)
    return {"ok": True, "chars": len(t)}


@app.get("/api/craft")
def api_craft_get():
    from app.caption.engine import craft
    return {"text": craft().strip()}


@app.post("/api/craft")
def api_craft_set(req: VoiceCoreUpdate):
    """The OPERATOR edits the craft/moves layer directly — this text sits in every v3 engine
    system prompt. Principles a caption draws on when it's that kind of caption, never
    universal rules (operator directive 2026-07-15)."""
    t = (req.text or "").strip()
    if len(t) < 100:
        raise HTTPException(status_code=400, detail="craft text suspiciously short — refusing")
    path = os.path.join("var", "craft.md")
    os.makedirs("var", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, path)
    return {"ok": True, "chars": len(t)}


class CharterUpdate(BaseModel):
    engine_id: str
    text: str


@app.get("/api/charters")
def api_charters_get():
    """The five v3 engine charters (operator-editable understanding docs, one per engine)."""
    from app.caption import charters as ch
    return {"engines": [{"id": e["id"], "name": e["name"], "charter": ch.charter(e["id"])}
                        for e in ch.ENGINES]}


@app.post("/api/charters")
def api_charters_set(req: CharterUpdate):
    """The OPERATOR edits an engine's charter directly — it IS that engine's understanding."""
    from app.caption import charters as ch
    try:
        n = ch.save_charter(req.engine_id, req.text)
    except KeyError as ex:
        raise HTTPException(status_code=404, detail=str(ex)) from ex
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    return {"ok": True, "chars": n}


class FormatBookUpdate(BaseModel):
    formats: list[dict]


@app.get("/api/formats")
def api_formats_get():
    """The FORMAT BOOK (validated caption vehicles + rotation state) — operator-inspectable."""
    from app.caption import formats
    return {"formats": formats.load_book(), "usage": formats._load_usage()}


@app.post("/api/formats")
def api_formats_set(req: FormatBookUpdate):
    """The OPERATOR edits the format book directly — verdicts, mechanisms, retired formats."""
    from app.caption import formats
    rows = [r for r in (req.formats or []) if isinstance(r, dict) and r.get("id") and r.get("skeleton")]
    if len(rows) < 5:
        raise HTTPException(status_code=400, detail="format book suspiciously small — refusing")
    return {"ok": True, "count": formats.save_book(rows)}


class SlateProbe(BaseModel):
    k: int = 5


@app.post("/api/debug/slate-probe")
def api_debug_slate_probe(req: SlateProbe):
    """Generate one PRODUCTION candidate slate (the exact reel path: generate_independent, produce-
    mode anchors) without rendering a reel — for verifying slate composition after rotation changes."""
    from app.caption.engine import generate_independent
    return {"candidates": generate_independent(k=max(1, min(8, req.k)))}


class RegenDecodes(BaseModel):
    write: bool = False
    fetch_report: bool = False


@app.post("/api/debug/regen-decodes")
def api_debug_regen_decodes(req: RegenDecodes):
    """Run the one-off decode split (scripts/regen_promoted_decodes.py) against the live volume —
    the script is the source of truth; this is just the execution vehicle. Dry-run unless
    {"write": true} (timestamped backup first; idempotent via decode_v). The run outlives the
    Railway edge timeout, so the report is persisted volume-side: poll {"fetch_report": true}."""
    from scripts.regen_promoted_decodes import last_report, run_all
    if req.fetch_report:
        rep = last_report()
        if rep is None:
            raise HTTPException(status_code=404, detail="no regen report yet")
        return rep
    return run_all(write=req.write)


class RelabelRefs(BaseModel):
    ref_ids: list[str]


@app.post("/api/debug/relabel-refs")
def api_debug_relabel_refs(req: RelabelRefs):
    """Re-decode why_it_works for the given refs with their source grading note folded in (the
    operator's own read on the line — punch-ups, one-off/comment-bait calls — outranks the LLM's)."""
    from app.corpus import promote
    return promote.relabel(req.ref_ids)


class GateCheck(BaseModel):
    texts: list[str]


@app.post("/api/debug/gate-check")
def api_debug_gate_check(req: GateCheck):
    """Replay the coherence gate over arbitrary caption texts WITHOUT touching generation — the
    validation harness for enabling drop mode (a graded round's mechanism-break kills must flag,
    its 8+ hits and the corpus must come back clean)."""
    from app.caption import engine
    texts = [str(t) for t in (req.texts or [])][:80]
    return {"n": len(texts), "broken": engine.check_coherence(texts)}


class CorpusRemove(BaseModel):
    ref_ids: list[str]


@app.post("/api/debug/corpus-remove")
def api_corpus_remove(req: CorpusRemove):
    """Surgically remove specific refs from the ACTIVE VOICE's corpus (operator-directed consolidation
    — e.g. same-joke renditions stacking a family's rotation slots). Not a low-score cull."""
    from app.corpus.store import load_refs
    pid = profiles.voice_id()
    path = profiles.corpus_path(pid)
    refs = load_refs(path)
    want = set(req.ref_ids)
    kept = [r for r in refs if r.get("ref_id") not in want]
    removed = [r.get("ref_id") for r in refs if r.get("ref_id") in want]
    if removed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return {"voice_profile_id": str(pid), "removed": removed, "total": len(kept)}


@app.post("/api/debug/corpus-dedup")
def api_corpus_dedup(dry: bool = True):
    """Remove NEAR-duplicate refs from the ACTIVE VOICE's corpus — the same joke promoted twice in
    different renditions gets double rotation slots + double priming (felt as format over-representation).
    Keeps the EARLIEST of each pair (originals outrank promotions). dry=true just reports the pairs."""
    from app.corpus.promote import _too_similar
    from app.corpus.store import load_refs
    pid = profiles.voice_id()
    path = profiles.corpus_path(pid)
    refs = load_refs(path)
    kept, removed = [], []
    for r in refs:
        cap = r.get("caption") or ""
        dup_of = next((k for k in kept if _too_similar(cap, k.get("caption") or "")), None)
        if dup_of is not None:
            removed.append({"ref_id": r.get("ref_id"), "caption": cap[:90],
                            "dup_of": dup_of.get("ref_id"), "kept_caption": (dup_of.get("caption") or "")[:90]})
        else:
            kept.append(r)
    if not dry and removed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return {"voice_profile_id": str(pid), "total": len(refs), "kept": len(kept),
            "removed": len(removed), "applied": bool(removed) and not dry, "pairs": removed}


@app.get("/api/refs/rotation")
def api_refs_rotation():
    """TRANSPARENCY: what the closed loop has done to each reference — keep/kill/best credits, usage,
    and its rotation status (amplified winner / de-weighted / normal). Nothing is ever dropped.
    Follows the ACTIVE VOICE pointer (rotation state belongs to the voice)."""
    from app import profiles
    from app.caption.engine import _load_json, _ref_key
    from app.corpus.store import load_refs
    pid = profiles.voice_id()
    scores = _load_json(profiles.ref_scores_path(pid))
    usage = _load_json(profiles.ref_usage_path(pid))
    out = []
    for r in load_refs(profiles.corpus_path(pid)):
        rid = r.get("ref_id") or ""
        s = scores.get(rid, {})
        k, x, b = s.get("keep", 0), s.get("kill", 0), s.get("best", 0)
        rate = (k + b) / (k + x) if (k + x) else None
        status = "normal"
        # thresholds mirror engine._pick_anchors is_winner/is_failer (era-fix 2026-07-06: the reel
        # era writes only keeps, so the old (k+x)>=6 gate showed amplified=[] forever)
        if (k + x) >= 2 and (rate or 0) >= 0.6:
            status = "amplified"
        elif rate is not None and rate < 0.25 and x >= 4 and x > k + 3:
            status = "de-weighted"
        out.append({"ref_id": rid, "trait": r.get("persona_trait"), "source": r.get("source"),
                    "keep": k, "kill": x, "best": b, "usage": usage.get(_ref_key(r), 0),
                    "status": status, "caption": (r.get("caption") or "")[:80]})
    return {"refs": out,
            "amplified": [r["ref_id"] for r in out if r["status"] == "amplified"],
            "de_weighted": [r["ref_id"] for r in out if r["status"] == "de-weighted"]}


# ── Living corpus: promote operator-validated bangers (9-10 reels) into the references ──
@app.get("/api/corpus/promotable")
def api_corpus_promotable(min_rating: int = 9):
    from app.corpus import promote
    return {"promotable": promote.promotable(min_rating=min_rating)}


class PromoteReq(BaseModel):
    reel_id: str


@app.post("/api/corpus/promote")
def api_corpus_promote(req: PromoteReq):
    from app.corpus import promote
    res = promote.promote(req.reel_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("reason", "promotion failed"))
    return res


@app.post("/api/chooser/eval")
def api_chooser_eval():
    """Replay the operator's own corrections ('you'd have posted X, not Y') against the CURRENT chooser —
    the objective selection-accuracy benchmark. Any future chooser change must not regress this."""
    import re as _re

    from app.caption.chooser import choose_best
    from app.corpus import grades as grade_store
    from app.corpus import reels

    def norm(t):
        return _re.sub(r"\s+", " ", (t or "")).strip().lower()

    pairs = [g for g in grade_store.load_grades() if g.get("type") == "pairwise"]
    recs = reels.graded()
    cases, correct, picked_loser, picked_other = 0, 0, 0, 0
    detail = []
    seen = set()
    for g in pairs:
        w, l = norm(g.get("winner")), norm(g.get("loser"))
        if not w or not l or (w, l) in seen:
            continue
        seen.add((w, l))
        rec = next((r for r in recs
                    if {w, l} <= {norm(c.get("text")) for c in (r.get("candidates") or [])}), None)
        if rec is None:
            continue
        cands = [c.get("text") or "" for c in rec.get("candidates") or []]
        try:
            pick = norm(choose_best(cands))
        except Exception:  # noqa: BLE001
            continue
        cases += 1
        verdict = "correct" if pick == w else ("picked_loser" if pick == l else "picked_other")
        correct += verdict == "correct"
        picked_loser += verdict == "picked_loser"
        picked_other += verdict == "picked_other"
        detail.append({"verdict": verdict, "should": (g.get("winner") or "")[:70], "picked": pick[:70]})
    return {"cases": cases, "correct": correct, "picked_loser": picked_loser, "picked_other": picked_other,
            "accuracy": round(correct / cases, 3) if cases else None, "detail": detail}


class ReelGrade(BaseModel):
    reel_id: str
    rating: int | None = None        # /10 quality rating on the finished reel
    notes: str | None = None         # free-text feedback (the primary signal)


@app.post("/api/reels/grade")
def api_reels_grade(req: ReelGrade, backend: str | None = None):
    from app.caption import backend as _bk
    from app.corpus import reels
    with _bk.using(backend):
        rec = reels.record_grade(req.reel_id, req.rating, (req.notes or "").strip() or None)
        if rec is None:
            raise HTTPException(status_code=404, detail="reel not found")
        try:    # AMPLIFY winners only. A strong rating credits the anchor a "keep" (weights it up in
                # rotation). We deliberately DON'T "kill" on a low rating: a weak reel is almost always a
                # delivery or a SELECTION miss, not a bad FORMAT — and culling formats for low scores
                # narrows the voice. Operator's standing rule: understand WHY it missed, never eliminate.
                # Credits go to the VOICE that generated the reel (recorded on it; fallback: current voice).
            anchors = rec.get("caption_anchor_refs") or []
            vpid = uuid.UUID(rec["voice_profile_id"]) if rec.get("voice_profile_id") else None
            if anchors and isinstance(req.rating, int) and req.rating >= 8:
                attribute.credit_verdict({"anchor_refs": anchors}, "keep", pid=vpid)
        except Exception:   # noqa: BLE001
            pass
        try:    # learn selection taste: if the note names a better candidate, capture the pairwise preference
            from app.caption import taste
            taste.learn_from_reel(rec)
        except Exception:   # noqa: BLE001
            pass
        try:    # PER-LANE ledger (2026-07-15): v3 candidates carry engine tags but nothing ever
                # aggregated them — CLAUDE.md's "grades accumulate per interaction lane" described
                # unimplemented wiring. Append-only observations, voice-owned; read via
                # GET /api/debug/lane-stats. Pure bookkeeping — nothing reads it into generation.
            import time as _t
            chosen_eng = next((c.get("engine") for c in (rec.get("candidates") or [])
                               if c.get("chosen") and c.get("engine")), None)
            if chosen_eng and isinstance(req.rating, int):
                lp = profiles.voice_file("lane_stats.jsonl", uuid.UUID(rec["voice_profile_id"])
                                         if rec.get("voice_profile_id") else None)
                with open(lp, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"engine": chosen_eng, "rating": req.rating,
                                        "reel_id": rec.get("reel_id"), "ts": _t.time()}) + "\n")
        except Exception:   # noqa: BLE001
            pass
        return {"ok": True}


@app.post("/api/captions/generate")
def api_captions_generate(req: CapGenRequest, craft: bool = False):
    try:
        from app.caption import engine  # lazy import (pulls anthropic + corpus)
        tok = engine._CRAFT.set(bool(craft))   # A/B: craft-deepened grounding (off by default)
        try:
            cands = engine.generate(notes=req.notes, n=req.n)
        finally:
            engine._CRAFT.reset(tok)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"generation failed: {exc}") from exc
    return {"candidates": cands}


@app.post("/api/captions/grade")
def api_captions_grade(req: GradeRequest):
    grade_store.record_verdict(req.caption, req.verdict, req.context, req.note)
    try:                                # close the loop: credit the anchor ref(s), per active profile
        attribute.credit_verdict(req.context, req.verdict)
    except Exception:                   # noqa: BLE001 — attribution must never break grading
        pass
    return {"ok": True}


@app.post("/api/captions/pairwise")
def api_captions_pairwise(req: PairRequest):
    grade_store.record_pairwise(req.winner, req.loser, req.context)
    return {"ok": True}


@app.post("/api/captions/best")
def api_captions_best(req: BestRequest):
    grade_store.record_best(req.winner, req.batch, req.context)
    try:                                # the chosen caption's anchor ref(s) get a 'best' credit
        attribute.credit_best(req.context)
    except Exception:                   # noqa: BLE001 — attribution must never break grading
        pass
    return {"ok": True}


@app.get("/api/captions/stats")
def api_captions_stats():
    g = grade_store.load_grades()
    verdicts = [x for x in g if x.get("type") == "verdict"]
    return {
        "total": len(g),
        "keeps": sum(1 for x in verdicts if x.get("verdict") == "keep"),
        "kills": sum(1 for x in verdicts if x.get("verdict") == "kill"),
        "off_voice": sum(1 for x in verdicts if x.get("verdict") == "off_voice"),
        "best": sum(1 for x in g if x.get("type") == "best"),
    }
