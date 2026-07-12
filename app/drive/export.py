"""Drive EXPORT — validated reels land in the operator's own My Drive, one folder per profile.

Tree: "{DRIVE_EXPORT_ROOT} / {profile name}" — created lazily (and best-effort at profile creation),
owned by the operator (OAuth), so it's immediately visible/shareable in their Drive. The folder id is
cached per profile on the volume; a deleted/trashed folder heals by re-creating on the next export.
"""
from __future__ import annotations

import json
import os

from app import profiles
from app.config import settings
from app.db import SessionLocal
from app.models import User


def _cache_path(pid) -> str:
    return profiles.voice_file("drive_export.json", pid)


def _profile_name(pid) -> str:
    with SessionLocal() as s:
        u = s.get(User, pid)
    return (u.handle if u is not None and u.handle else str(pid))[:100]


def ensure_profile_folder(pid) -> str:
    """The profile's Drive export folder id (create root + profile folder if needed; heal if deleted)."""
    from app.drive import gdrive
    svc = gdrive._user_service()
    cached = None
    try:
        with open(_cache_path(pid), encoding="utf-8") as f:
            cached = (json.load(f) or {}).get("folder_id")
    except Exception:  # noqa: BLE001
        cached = None
    if cached:
        try:
            meta = svc.files().get(fileId=cached, fields="id,trashed").execute()
            if not meta.get("trashed"):
                return cached
        except Exception:  # noqa: BLE001 — deleted out-of-band -> re-create below
            pass
    root_id = gdrive.ensure_folder(svc, settings.drive_export_root)
    fid = gdrive.ensure_folder(svc, _profile_name(pid), root_id)
    p = _cache_path(pid)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"folder_id": fid}, f)
    return fid


def upload_reference(pid, mp4_path: str, stem: str) -> dict:
    """Upload a REFERENCE RECREATION into '<profile folder>/references' (created lazily). Same
    OAuth-as-operator flow as validated exports; separate subfolder so recreations never mix
    with validated originals."""
    from app.drive import gdrive
    svc = gdrive._user_service()
    profile_fid = ensure_profile_folder(pid)
    ref_fid = gdrive.ensure_folder(svc, "references", profile_fid)
    up = gdrive.upload_file(svc, mp4_path, ref_fid, name=stem + ".mp4")
    return {"link": up.get("link"), "file_id": up.get("id"), "folder_id": ref_fid}


def upload_validated(pid, mp4_path: str, stem: str, caption: str | None = None) -> dict:
    """Upload a validated reel into the profile's export folder. Just the mp4 — the caption is baked
    into the video and logged in validated.jsonl; sidecar files only cluttered the folder. Returns
    {link, file_id, folder_id}."""
    from app.drive import gdrive
    svc = gdrive._user_service()
    fid = ensure_profile_folder(pid)
    up = gdrive.upload_file(svc, mp4_path, fid, name=stem + ".mp4")
    return {"link": up.get("link"), "file_id": up.get("id"), "folder_id": fid}
