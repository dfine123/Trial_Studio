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

from app import schemas
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Audio, Clip, ClipFolder, User
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


class GenerateRequest(BaseModel):
    audio_id: uuid.UUID | None = None
    notes: str | None = None
    folder_id: uuid.UUID | None = None   # restrict generation to this folder (+ its sub-folders)


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


# ── Clip library + upload (treelz.ai) ─────────────────────────
@app.get("/api/clips/library")
def api_clips_library(folder_id: str | None = None):
    """Clips with tags + status, newest first. folder_id filters the view: a folder id shows clips
    DIRECTLY in it, 'none' shows unfiled clips, omitted shows all."""
    with SessionLocal() as s:
        q = select(Clip).options(selectinload(Clip.segments)).order_by(Clip.created_at.desc())
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
    user_id = ensure_default_user()
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
        rows = s.scalars(select(ClipFolder)).all()
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
        clips = s.scalars(select(Clip).where(Clip.folder_id.in_(ids), Clip.status == "indexed")).all()
    return [str(c.id) for c in clips]


@app.get("/api/folders")
def api_folders():
    """All folders (flat: id, name, parent_id, direct clip count). The UI builds the tree."""
    with SessionLocal() as s:
        folders = s.scalars(select(ClipFolder).order_by(ClipFolder.name)).all()
        counts: dict = {}
        for (fid,) in s.execute(select(Clip.folder_id).where(Clip.folder_id.isnot(None))).all():
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
        f = ClipFolder(user_id=ensure_default_user(),
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
    """Delete a clip + its segments (cascade), best-effort removing the uploaded file."""
    with SessionLocal() as s:
        c = s.get(Clip, clip_id)
        if c is not None:
            path = c.r2_key
            s.delete(c)
            s.commit()
            if path and os.path.isabs(path) and os.path.exists(path):  # only our uploads, not sample basenames
                try:
                    os.remove(path)
                except OSError:
                    pass
    return {"ok": True}


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


# ── treelz.ai web app ─────────────────────────────────────────
@app.get("/login")
def login_page():
    return FileResponse(os.path.join(_WEB_DIR, "login.html"))


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

    audio_path = os.path.join("samples", "audio", os.path.basename(audio.r2_key))
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail=f"audio file missing locally: {audio_path}")

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
                            clip_ids=clip_ids)
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
