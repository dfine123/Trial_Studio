"""Profiles = creators. The platform's core organizing unit.

Each profile OWNS its clips, folders, and VOICE (caption corpus + grading + generation log). Templates
and the audio library are SHARED across profiles (a formula is creator-agnostic; trending sounds are
universal). A single 'active profile' — persisted on the volume so it survives redeploys and is visible
to backgrounded generation — scopes the clip/folder queries and resolves every voice file path.

A profile is a `User` row (the schema was user-scoped from day one). The first user is the 'Spence'
profile; its voice files are migrated out of the pre-profiles global locations on first boot.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User

_ACTIVE_PATH = os.path.join("var", "active_profile.json")

# pre-profiles global voice files -> migrated into the first ('Spence') profile's dir once
_LEGACY = {
    "references.jsonl": os.path.join("corpus", "references.jsonl"),
    "generated.jsonl": os.path.join("corpus", "generated.jsonl"),
    "ref_usage.json": os.path.join("var", "ref_usage.json"),
    "ref_scores.json": os.path.join("var", "ref_scores.json"),
    "grades.jsonl": os.path.join("var", "grades.jsonl"),
}

_default_id: uuid.UUID | None = None   # cached first-profile id (the fallback when nothing is active)


def profile_dir(pid: uuid.UUID) -> str:
    d = os.path.join("var", "profiles", str(pid))
    os.makedirs(d, exist_ok=True)
    return d


def voice_file(name: str, pid: uuid.UUID | None = None) -> str:
    return os.path.join(profile_dir(pid or active_id()), name)


# resolvers used across the voice stack (store / engine / genlog / grades)
def corpus_path(pid: uuid.UUID | None = None) -> str:    return voice_file("references.jsonl", pid)
def genlog_path(pid: uuid.UUID | None = None) -> str:    return voice_file("generated.jsonl", pid)
def ref_usage_path(pid: uuid.UUID | None = None) -> str: return voice_file("ref_usage.json", pid)
def ref_scores_path(pid: uuid.UUID | None = None) -> str: return voice_file("ref_scores.json", pid)
def grades_path(pid: uuid.UUID | None = None) -> str:    return voice_file("grades.jsonl", pid)


def _seed_profile_voice(pid: uuid.UUID) -> None:
    """One-time: copy the pre-profiles global voice files into this profile's dir (if not already there)."""
    d = profile_dir(pid)
    for name, legacy in _LEGACY.items():
        dst = os.path.join(d, name)
        if not os.path.exists(dst) and os.path.exists(legacy):
            shutil.copyfile(legacy, dst)


def ensure_default_profile() -> uuid.UUID:
    """The first user IS the 'Spence' profile; name it + migrate its voice once. Returns its id."""
    global _default_id
    with SessionLocal() as s:
        u = s.scalar(select(User).order_by(User.created_at).limit(1))
        if u is None:
            u = User(handle="Spence", description="Spence — comedy voice (first profile)")
            s.add(u)
            s.commit()
            s.refresh(u)
        elif (u.handle or "").strip().lower() in ("", "default", "test", "user"):
            u.handle = "Spence"   # the established first profile IS Spence (placeholder handle -> name it)
            s.commit()
        _default_id = u.id
    _seed_profile_voice(_default_id)
    return _default_id


def active_id() -> uuid.UUID:
    """The profile everything is scoped to right now (persisted), or the default profile."""
    global _default_id
    if _default_id is None:
        ensure_default_profile()
    try:
        with open(_ACTIVE_PATH, encoding="utf-8") as f:
            return uuid.UUID(json.load(f)["id"])
    except Exception:  # noqa: BLE001 — missing/corrupt -> fall back to the default profile
        return _default_id


def set_active(pid: uuid.UUID) -> None:
    os.makedirs("var", exist_ok=True)
    tmp = _ACTIVE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": str(pid)}, f)
    os.replace(tmp, _ACTIVE_PATH)


def list_profiles() -> list[dict]:
    act = active_id()
    with SessionLocal() as s:
        rows = s.scalars(select(User).order_by(User.created_at)).all()
    return [{"id": str(u.id), "name": u.handle or "Untitled", "niche": u.niche,
             "active": u.id == act} for u in rows]


def create_profile(name: str, niche: str | None = None) -> dict:
    with SessionLocal() as s:
        u = User(handle=(name or "Untitled").strip()[:255] or "Untitled", niche=(niche or None))
        s.add(u)
        s.commit()
        s.refresh(u)
        pid = u.id
    profile_dir(pid)   # create the (empty) voice dir so a fresh profile starts with its own blank voice
    return {"id": str(pid), "name": u.handle, "niche": u.niche, "active": False}


def delete_profile(pid: uuid.UUID) -> None:
    """Remove a profile (and its scoped clips/folders cascade via FK). Refuses the last one; resets
    active to the default if the deleted one was active. Voice dir is left on disk (cheap, harmless)."""
    with SessionLocal() as s:
        n = s.scalar(select(User).order_by(User.created_at).limit(1))  # keep at least one
        count = len(s.scalars(select(User)).all())
        u = s.get(User, pid)
        if u is None:
            return
        if count <= 1:
            raise ValueError("can't delete the only profile")
        s.delete(u)
        s.commit()
    if active_id() == pid:
        set_active(ensure_default_profile())
