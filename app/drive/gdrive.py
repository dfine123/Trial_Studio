"""Google Drive ingest connector — READ-ONLY access via a SERVICE ACCOUNT.

The model shares their content folder with the service-account email (Viewer); no OAuth, no Drive
scope verification, content stays private. Credentials come from settings.google_sa_info
(GOOGLE_SA_JSON contents on Railway, or GOOGLE_SA_JSON_FILE path locally).

This is purely the discovery + download layer. New video files it finds are handed to the EXISTING
`index_source` pipeline (QC -> whole/split -> analyze -> clip_role -> shoots) by the sync worker.
"""
from __future__ import annotations

import os
import re

from app.config import settings

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_FOLDER_MIME = "application/vnd.google-apps.folder"
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]+$")   # Drive ids are this charset; reject anything else (no q= injection)


class DriveNotConfigured(RuntimeError):
    """No service-account credentials are configured on this environment."""


class DriveAccessError(RuntimeError):
    """An invalid folder id or an access failure."""


def _service():
    info = settings.google_sa_info
    if not info:
        raise DriveNotConfigured("no service-account credentials — set GOOGLE_SA_JSON (Railway) or "
                                 "GOOGLE_SA_JSON_FILE (local path to the key)")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def folder_id_from(link_or_id: str) -> str:
    """Accept a raw folder ID or any Drive folder URL and return the bare ID."""
    s = (link_or_id or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", s)
    return m.group(1) if m else s


def verify_access(folder_id: str) -> dict:
    """Confirm the SA can actually see the folder (i.e. the model really shared it with our email).
    Returns {ok: bool, name?: str, error?: str} — used at connect time for instant feedback."""
    if not _VALID_ID.match(folder_id or ""):
        return {"ok": False, "error": "that doesn't look like a valid Drive folder link or id"}
    try:
        meta = _service().files().get(
            fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True).execute()
    except DriveNotConfigured as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface "not shared / not found" to the UI plainly
        return {"ok": False, "error": "can't access that folder — is it shared with the service "
                f"account ({settings.google_sa_email or 'the SA email'})? [{type(exc).__name__}]"}
    if meta.get("mimeType") != _FOLDER_MIME:
        return {"ok": False, "error": "that link points to a file, not a folder"}
    return {"ok": True, "name": meta.get("name"), "folder_id": meta.get("id")}


def list_videos(folder_id: str, since: str | None = None, root_name: str | None = None) -> list[dict]:
    """Recursively list video files under `folder_id` (agencies nest by shoot subfolder). Each item:
    {id, name, mimeType, size, modifiedTime, folder} where `folder` is the immediate parent folder's
    NAME — the creator's own label (e.g. "lipsyncs", "pre glow up"), fed to the classifier as a prior.
    `since` (RFC3339) keeps only files modified after the last sync, so polling is incremental."""
    if not _VALID_ID.match(folder_id or ""):
        raise DriveAccessError(f"invalid folder id: {folder_id!r}")
    svc = _service()
    out: list[dict] = []
    stack: list[tuple[str, str | None]] = [(folder_id, root_name)]   # (id, this folder's name)
    seen: set[str] = set()
    while stack:
        fid, fname = stack.pop()
        if fid in seen:
            continue
        seen.add(fid)
        page = None
        while True:
            resp = svc.files().list(
                q=f"'{fid}' in parents and trashed = false", spaces="drive",
                fields="nextPageToken, files(id,name,mimeType,size,modifiedTime,"
                       "videoMediaMetadata(durationMillis))",
                pageSize=200, pageToken=page,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
            ).execute()
            for f in resp.get("files", []):
                mt = f.get("mimeType", "")
                if mt == _FOLDER_MIME:
                    stack.append((f["id"], f.get("name")))
                elif mt.startswith("video/"):
                    if since and f.get("modifiedTime") and f["modifiedTime"] <= since:
                        continue
                    out.append({**f, "folder": fname})
            page = resp.get("nextPageToken")
            if not page:
                break
    return out


def download(file_id: str, dest: str) -> str:
    """Stream a Drive file to `dest` in chunks (handles large 4K clips without loading into memory)."""
    from googleapiclient.http import MediaIoBaseDownload
    svc = _service()
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest
