"""DEMO MODE — the friends-demo deployment (same repo, second Railway service, DEMO_MODE=1).

Flow: open signup -> upload clips (capped) -> clips index -> generate reels, all generating with
the seeded BASE voice. Per user: settings.demo_max_clips clips, settings.demo_reels_per_window
reels per window; hitting the reel cap starts a demo_cooldown_hours cooldown, after which the
counter fully resets (repeatable demo).

Isolation model: every signup is its own User/profile row — the EXISTING profile scoping
(clips, folders, reels, voice pointer) does all the work once profiles.active_id() resolves to
the session user (request-scoped ContextVar set by the middleware below). The demo service runs
on its own database + volume, so nothing here can touch production data. A path WHITELIST (not a
blacklist) locks the service down to exactly the demo surface — operator pages, debug endpoints,
drive, lab, grading, corpus tooling are all unreachable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import func, select

from app import profiles
from app.config import settings
from app.db import SessionLocal
from app.models import Audio, Clip, User

_COOKIE = "demo_session"
_BASE_HANDLE = "__demo_base__"          # the seeded Base-voice profile; not signup-able (signup regex forbids it)
_SEED_DIR = os.path.join("corpus", "demo_base")
_base_id: uuid.UUID | None = None

_USERNAME_RX = re.compile(r"^[a-z0-9_]{3,24}$")


# ── passwords (stdlib pbkdf2 — no new deps) ─────────────────────────────────
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    try:
        _, salt, want = (stored or "").split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
        return hmac.compare_digest(dk.hex(), want)
    except Exception:  # noqa: BLE001
        return False


# ── session cookie: uid.exp.sig (hmac over uid.exp with the treelz secret) ──
def mint_session(uid: uuid.UUID, days: int = 30) -> str:
    exp = int(time.time()) + days * 86400
    body = f"{uid.hex}.{exp}"
    sig = hmac.new(settings.treelz_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def session_uid(request: Request) -> uuid.UUID | None:
    tok = request.cookies.get(_COOKIE) or ""
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    body = f"{parts[0]}.{parts[1]}"
    want = hmac.new(settings.treelz_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[2], want):
        return None
    try:
        if int(parts[1]) < time.time():
            return None
        return uuid.UUID(hex=parts[0])
    except (ValueError, TypeError):
        return None


# ── accounts ─────────────────────────────────────────────────────────────────
def signup(username: str, password: str) -> uuid.UUID:
    uname = (username or "").strip().lower()
    if not _USERNAME_RX.fullmatch(uname):
        raise HTTPException(status_code=400, detail="username: 3-24 chars, letters/numbers/underscores")
    if len(password or "") < 6:
        raise HTTPException(status_code=400, detail="password: at least 6 characters")
    with SessionLocal() as s:
        taken = s.scalar(select(User).where(func.lower(User.handle) == uname))
        if taken is not None:
            raise HTTPException(status_code=409, detail="that username is taken")
        u = User(handle=uname, password_hash=hash_password(password), description="demo account")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    profiles.set_voice(uid, ensure_demo_base())   # every demo account generates with the Base voice
    return uid


def login(username: str, password: str) -> uuid.UUID:
    uname = (username or "").strip().lower()
    with SessionLocal() as s:
        u = s.scalar(select(User).where(func.lower(User.handle) == uname))
        if u is None or not u.password_hash or not verify_password(password, u.password_hash):
            raise HTTPException(status_code=401, detail="wrong username or password")
        return u.id


# ── the Base voice + audio library, seeded at boot ───────────────────────────
def ensure_demo_base() -> uuid.UUID:
    """The shared Base-voice profile: created once, corpus/persona seeded from the committed
    export (corpus/demo_base/). Demo accounts point their voice at it (read AND anti-repeat
    state live in its dir — shared across demo users by design)."""
    global _base_id
    if _base_id is not None:
        return _base_id
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.handle == _BASE_HANDLE))
        if u is None:
            u = User(handle=_BASE_HANDLE, voice_label="Base", description="the demo's shared Base voice")
            s.add(u)
            s.commit()
            s.refresh(u)
        _base_id = u.id
    d = profiles.profile_dir(_base_id)
    for name in ("references.jsonl", "persona.md"):
        dst = os.path.join(d, name)
        src = os.path.join(_SEED_DIR, name)
        if not os.path.exists(dst) and os.path.exists(src):
            with open(src, encoding="utf-8") as fsrc, open(dst, "w", encoding="utf-8") as fdst:
                fdst.write(fsrc.read())
    return _base_id


def boot() -> None:
    """DEMO service boot: seed the Base voice + the audio library (idempotent)."""
    ensure_demo_base()
    try:
        with SessionLocal() as s:
            have_audio = s.scalar(select(func.count()).select_from(Audio)) or 0
        if not have_audio:
            from app.seed.seed_audio import seed
            seed()
    except Exception as exc:  # noqa: BLE001 — audio seeding must never block boot
        print(f"[demo] audio seed skipped: {exc}", flush=True)


# ── per-user caps ─────────────────────────────────────────────────────────────
def clips_used(uid: uuid.UUID) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Clip)
                        .where(Clip.user_id == uid, Clip.status != "rejected")) or 0


def _quota_path(uid: uuid.UUID) -> str:
    return os.path.join(profiles.profile_dir(uid), "demo_quota.json")


def _load_quota(uid: uuid.UUID) -> dict:
    try:
        with open(_quota_path(uid), encoding="utf-8") as f:
            q = json.load(f)
    except Exception:  # noqa: BLE001
        q = {"count": 0, "cooldown_until": None}
    cd = q.get("cooldown_until")
    if cd and time.time() >= cd:                 # cooldown over -> the counter fully resets
        q = {"count": 0, "cooldown_until": None}
    return q


def _save_quota(uid: uuid.UUID, q: dict) -> None:
    p = _quota_path(uid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f)
    os.replace(tmp, p)


def quota_state(uid: uuid.UUID) -> dict:
    q = _load_quota(uid)
    cd = q.get("cooldown_until")
    return {
        "reels_used": q.get("count", 0),
        "reels_max": settings.demo_reels_per_window,
        "cooldown_until": cd,
        "resets_in_seconds": max(0, int(cd - time.time())) if cd else None,
        "can_generate": (not cd) and q.get("count", 0) < settings.demo_reels_per_window,
    }


def check_quota(uid: uuid.UUID) -> None:
    st = quota_state(uid)
    if not st["can_generate"]:
        wait = st["resets_in_seconds"] or int(settings.demo_cooldown_hours * 3600)
        raise HTTPException(status_code=429,
                            detail=f"reel limit reached — resets in {wait // 3600}h {(wait % 3600) // 60}m",
                            headers={"Retry-After": str(wait)})


def count_reel(uid: uuid.UUID) -> dict:
    """Called AFTER a successful generation (failures never consume quota). Hitting the cap
    starts the cooldown."""
    q = _load_quota(uid)
    q["count"] = q.get("count", 0) + 1
    if q["count"] >= settings.demo_reels_per_window and not q.get("cooldown_until"):
        q["cooldown_until"] = time.time() + settings.demo_cooldown_hours * 3600
    _save_quota(uid, q)
    return quota_state(uid)


# ── the route whitelist (everything else 404s on the demo service) ───────────
# /admin + /api/admin/* + /api/login are OPEN at the router level but gate themselves on the
# OPERATOR cookie (env creds) — a demo session can never satisfy them.
_OPEN_EXACT = {"/", "/health", "/api/demo/signup", "/api/demo/login", "/admin", "/api/login"}
_OPEN_PREFIX = ("/assets/", "/api/admin/")
_AUTH_EXACT = {"/api/demo/logout", "/api/demo/me", "/api/demo/status", "/api/demo/reels",
               "/api/clips/upload", "/api/clips/library", "/api/generate"}
_AUTH_PREFIX = ("/reels/",)


def route_allowed(method: str, path: str) -> tuple[bool, bool]:
    """(allowed, needs_auth) for a request on the demo service."""
    if path in _OPEN_EXACT or any(path.startswith(p) for p in _OPEN_PREFIX):
        return True, False
    if path in _AUTH_EXACT or any(path.startswith(p) for p in _AUTH_PREFIX):
        return True, True
    # per-clip endpoints: GET status/thumb + DELETE only (ownership enforced in the handlers)
    if path.startswith("/api/clips/") and method in ("GET", "DELETE"):
        return True, True
    return False, False
