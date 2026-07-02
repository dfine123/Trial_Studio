"""Drive sync — turn a connected (shared, read-only) Drive folder into indexed clips for a profile.

  connect()         verify the SA can see the folder + register the connection
  sync_connection() list new videos -> download -> the EXACT upload pipeline (Clip row + run_pipeline
                    under the indexing semaphore) -> SyncedFile ledger
  status()          per-connection counts + the service-account email to share folders with

Read-only on the Drive side; incremental + idempotent via the SyncedFile ledger. Each Drive subfolder
maps to a ClipFolder of the same name, so the creator's own organization carries over.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone

from app import models
from app.config import settings
from app.db import SessionLocal

SIZE_CAP_BYTES = 4 * 1024 ** 3   # skip files bigger than this — one huge file shouldn't fill the disk
DEFAULT_MAX_FILES = 50           # cap one sync pass; re-sync pulls the next batch


def _safe_ext(name: str | None) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext or "") else ".mp4"


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def connect(user_id: uuid.UUID, link_or_id: str, log=print) -> dict:
    """Verify access then register/refresh the connection. {ok, connection_id?, folder_name?, error?}."""
    from app.drive import gdrive
    fid = gdrive.folder_id_from(link_or_id)
    acc = gdrive.verify_access(fid)
    if not acc.get("ok"):
        return {"ok": False, "error": acc.get("error")}
    with SessionLocal() as s:
        conn = s.query(models.DriveConnection).filter_by(user_id=user_id, folder_id=fid).first()
        if conn is None:
            conn = models.DriveConnection(user_id=user_id, provider="gdrive", folder_id=fid,
                                          folder_name=acc.get("name"), status="connected")
            s.add(conn)
        else:
            conn.folder_name, conn.status, conn.last_error = acc.get("name"), "connected", None
        s.commit()
        s.refresh(conn)
        return {"ok": True, "connection_id": str(conn.id), "folder_name": conn.folder_name}


def _folder_for(user_id: uuid.UUID, name: str | None) -> uuid.UUID | None:
    """Get-or-create a ClipFolder matching the Drive subfolder name (the creator's own organization)."""
    label = (name or "").strip()
    if not label:
        return None
    with SessionLocal() as s:
        f = s.query(models.ClipFolder).filter_by(user_id=user_id, name=label[:255]).first()
        if f is None:
            f = models.ClipFolder(user_id=user_id, name=label[:255])
            s.add(f)
            s.commit()
            s.refresh(f)
        return f.id


def sync_connection(connection_id, max_files: int | None = DEFAULT_MAX_FILES, log=print) -> dict:
    """Pull up to `max_files` NEW videos through the standard upload->index pipeline. Idempotent via
    the ledger; refuses to run concurrently with itself; always leaves status non-'syncing'."""
    from sqlalchemy import update

    from app.drive import gdrive

    with SessionLocal() as s:   # atomically CLAIM the sync — only one runs per connection
        conn = s.get(models.DriveConnection, connection_id)
        if conn is None:
            return {"error": "no such connection"}
        user_id, folder_id, folder_name = conn.user_id, conn.folder_id, conn.folder_name
        claimed = s.execute(update(models.DriveConnection)
                            .where(models.DriveConnection.id == connection_id,
                                   models.DriveConnection.status != "syncing")
                            .values(status="syncing")).rowcount
        s.commit()
        if not claimed:
            return {"busy": True, "error": "a sync is already running for this connection"}
        seen = {f.provider_file_id for f in
                s.query(models.SyncedFile).filter_by(connection_id=connection_id).all()}

    summary = {"new": 0, "clips": 0, "rejected": 0, "failed": 0, "skipped_large": 0, "remaining": 0}
    try:
        vids = gdrive.list_videos(folder_id, root_name=folder_name)
        pending = [v for v in vids if v["id"] not in seen]

        def _shortest_first(v: dict) -> tuple:
            """Sort key: real duration when Drive has processed it, else file size. Short clips download
            + index fastest, so the library fills with usable clips early on a big folder."""
            try:
                d = int((v.get("videoMediaMetadata") or {}).get("durationMillis") or 0)
            except (TypeError, ValueError):
                d = 0
            return (0, d) if d > 0 else (1, int(v.get("size") or 0))

        pending.sort(key=_shortest_first)
        new = pending[:max_files] if max_files else pending
        summary["new"], summary["remaining"] = len(new), max(0, len(pending) - len(new))
        log(f"[drive] sync {str(connection_id)[:8]}: {len(new)} of {len(pending)} new (cap {max_files})")
        os.makedirs("var/uploads", exist_ok=True)

        def _one(v: dict) -> tuple[str, int]:
            """Download + index ONE file, write its ledger row. Returns (status, n_clips). Several run
            concurrently — the long TL remote waits overlap; cv2 serializes inside the pipeline."""
            clip_ids: list[str] = []
            status, reason = "failed", None
            if int(v.get("size") or 0) > SIZE_CAP_BYTES:
                status, reason = "skipped_large", "file too large (> 4GB) — skipped"
            else:
                clip_id = uuid.uuid4()
                dest = os.path.abspath(os.path.join("var/uploads", f"{clip_id}{_safe_ext(v.get('name'))}"))
                try:
                    gdrive.download(v["id"], dest)
                    fid = None
                    if (v.get("folder") or "") != (folder_name or ""):   # subfolder -> matching ClipFolder
                        fid = _folder_for(user_id, v.get("folder"))
                    with SessionLocal() as s:
                        s.add(models.Clip(id=clip_id, user_id=user_id, r2_key=dest,
                                          status="uploaded", folder_id=fid))
                        s.commit()
                    from app.indexing.pipeline import run_pipeline   # heavy import — in the worker
                    from app.main import _INDEX_SEM                  # bounds clips in flight (3)
                    with _INDEX_SEM:
                        run_pipeline(clip_id, source_path=dest)
                    with SessionLocal() as s:
                        c = s.get(models.Clip, clip_id)
                        if c is not None and c.status == "indexed":
                            status, reason, clip_ids = "synced", None, [str(clip_id)]
                        else:
                            status = "rejected"
                            reason = (c.rejection_reason if c is not None else None) or "not indexed"
                except Exception as exc:  # noqa: BLE001 — one file must not stop the sync
                    status, reason = "failed", str(exc)[:200]
                    _rm(dest)
                    log(f"[drive]   FAILED {v.get('name')}: {exc}")
            with SessionLocal() as s:
                s.add(models.SyncedFile(connection_id=connection_id, user_id=user_id,
                                        provider_file_id=v["id"], name=v.get("name"),
                                        status="failed" if status == "skipped_large" else status,
                                        reason=reason, clip_ids=clip_ids))
                s.commit()
            return status, len(clip_ids)

        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, settings.index_concurrency)      # in flight: TL waits overlap, cv2 serialized
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for status, n_clips in pool.map(_one, new):
                summary["clips"] += n_clips
                summary["rejected"] += 1 if status == "rejected" else 0
                summary["failed"] += 1 if status == "failed" else 0
                summary["skipped_large"] += 1 if status == "skipped_large" else 0
        return summary
    except Exception as exc:  # noqa: BLE001 — surface a top-level failure on the connection
        log(f"[drive] sync error: {exc}")
        summary["error"] = str(exc)[:300]
        return summary
    finally:                                       # ALWAYS release the sync claim, even on crash
        with SessionLocal() as s:
            conn = s.get(models.DriveConnection, connection_id)
            if conn is not None:
                conn.status = "connected"
                conn.last_synced_at = datetime.now(timezone.utc)
                conn.last_error = summary.get("error")
                s.commit()


def status(user_id: uuid.UUID) -> dict:
    """Per-connection summary for the UI, plus the email to share folders with."""
    with SessionLocal() as s:
        out = []
        for c in s.query(models.DriveConnection).filter_by(user_id=user_id).all():
            files = s.query(models.SyncedFile).filter_by(connection_id=c.id).all()
            out.append({
                "connection_id": str(c.id), "folder_name": c.folder_name, "folder_id": c.folder_id,
                "status": c.status,
                "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
                "last_error": c.last_error, "files_seen": len(files),
                "imported_files": sum(1 for f in files if f.status == "synced"),
                "clips": sum(len(f.clip_ids or []) for f in files),
                "rejected": sum(1 for f in files if f.status == "rejected"),
                "failed": sum(1 for f in files if f.status == "failed"),
            })
    return {"connections": out, "service_account": settings.google_sa_email,
            "configured": settings.google_sa_info is not None}
