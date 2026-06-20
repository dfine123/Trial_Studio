"""Twelve Labs integration — SDK v1.2.8.

Surface verified against the installed SDK and the official README (June 2026):
  - client.indexes.create(index_name=..., models=[{model_name, model_options}]) -> .id
  - client.indexes.list(index_name=...) -> pager of IndexSchema(.id, .index_name)
  - client.tasks.create(index_id=..., video_file=path | video_url=url) -> .id/.video_id
  - client.tasks.retrieve(task_id) -> .status (ready|failed|indexing|...), .video_id
  - client.analyze(video_id=..., model_name="pegasus1.2", prompt=...) -> .data (text)
  - client.embed.tasks.create(model_name="Marengo-retrieval-2.7", video_file|video_url,
        video_embedding_scope=["video"]) -> poll -> .retrieve() -> .video_embedding.segments

Gotcha #4: indexing is async — poll task status to `ready` (backoff + timeout) before the
video is queryable; on `failed` raise so the clip is marked rejected.
"""
from __future__ import annotations

import json
import os
import time

from twelvelabs import TwelveLabs

from app.config import settings

_READY = "ready"
_FAILED = "failed"


class TwelveLabsError(RuntimeError):
    pass


def client() -> TwelveLabs:
    if not settings.twelvelabs_api_key:
        raise TwelveLabsError("TWELVELABS_API_KEY is not set")
    return TwelveLabs(api_key=settings.twelvelabs_api_key)


# ── Index ────────────────────────────────────────────────────
def ensure_index(c: TwelveLabs, name: str | None = None) -> str:
    """Return the id of the named index, creating it (Marengo + Pegasus) if absent."""
    name = name or settings.twelvelabs_index_name
    try:
        for idx in c.indexes.list(index_name=name, page_limit=10):
            if getattr(idx, "index_name", None) == name:
                return idx.id
    except Exception:  # noqa: BLE001 — listing is a convenience; fall through to create
        pass

    marengo_candidates = [settings.tl_marengo_model, "marengo3.0", "marengo2.7"]
    last_err: Exception | None = None
    seen: set[str] = set()
    for marengo in marengo_candidates:
        if marengo in seen:
            continue
        seen.add(marengo)
        try:
            resp = c.indexes.create(
                index_name=name,
                models=[
                    {"model_name": marengo, "model_options": ["visual", "audio"]},
                    {"model_name": settings.tl_pegasus_model, "model_options": ["visual", "audio"]},
                ],
            )
            return resp.id
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise TwelveLabsError(f"could not create index {name!r}: {last_err}")


# ── Upload + poll (async indexing) ───────────────────────────
def index_video(
    c: TwelveLabs,
    index_id: str,
    *,
    video_file: str | None = None,
    video_url: str | None = None,
    poll_timeout: float = 1800.0,
    initial_interval: float = 5.0,
    max_interval: float = 20.0,
):
    """Create an indexing task and poll until ready. Returns the ready task (has .video_id)."""
    create_kwargs: dict = {"index_id": index_id}
    fh = None
    if video_file is not None:
        if isinstance(video_file, str):
            fh = open(video_file, "rb")  # SDK needs a file handle, not a path string
            create_kwargs["video_file"] = (os.path.basename(video_file), fh, "video/mp4")
        else:
            create_kwargs["video_file"] = video_file
    if video_url is not None:
        create_kwargs["video_url"] = video_url  # omit unused source — passing None is rejected
    try:
        task = c.tasks.create(**create_kwargs)
    finally:
        if fh is not None:
            fh.close()
    task_id = task.id

    deadline = time.monotonic() + poll_timeout
    interval = initial_interval
    status = None
    while time.monotonic() < deadline:
        t = c.tasks.retrieve(task_id)
        status = t.status
        if status == _READY:
            return t
        if status == _FAILED:
            raise TwelveLabsError(f"indexing task {task_id} failed (status={status})")
        time.sleep(interval)
        interval = min(interval * 1.5, max_interval)  # backoff
    raise TwelveLabsError(f"indexing task {task_id} timed out after {poll_timeout}s (last status={status})")


# ── Pegasus analysis ─────────────────────────────────────────
_PEGASUS_PROMPT = """You are indexing a short vertical lifestyle video clip for a reels editor.
Return ONLY valid JSON (no markdown fences, no prose) with EXACTLY this shape:
{
  "summary": "1-3 sentence overall summary of the clip",
  "setting": "where it takes place (e.g. city street, penthouse, beach, car interior)",
  "time_of_day": "day | night | golden_hour | unknown",
  "lighting_tags": ["lowercase lighting descriptors"],
  "vibe_tags": ["lowercase mood/aesthetic descriptors"],
  "camera_movement": "static | pan | handheld | tracking | zoom | unknown",
  "has_speech": true,
  "has_music": false,
  "moments": [
    {"start": 0.0, "end": 0.0, "description": "what happens in this moment", "energy": 0.5}
  ]
}
Provide several moments that cover the whole timeline in order. energy is 0..1 (calm..intense)."""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {"summary": text[:1000], "moments": []}


def analyze_clip(
    c: TwelveLabs, video_id: str, max_tokens: int = 1400, real_duration: float | None = None
) -> dict:
    prompt = _PEGASUS_PROMPT
    if real_duration:
        prompt += (
            f"\n\nIMPORTANT: only the first {real_duration:.1f} seconds contain real footage; "
            f"the remainder is a frozen hold of the last frame. Describe ONLY the first "
            f"{real_duration:.1f}s and keep every moment timestamp within it."
        )
    res = c.analyze(
        video_id=video_id,
        model_name=settings.tl_pegasus_model,
        prompt=prompt,
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return _parse_json(getattr(res, "data", "") or "")


# ── Marengo per-clip embedding (best-effort) ─────────────────
def embed_video(
    c: TwelveLabs,
    *,
    video_file: str | None = None,
    video_url: str | None = None,
    model_name: str = "marengo3.0",
    poll_timeout: float = 1800.0,
) -> list[float] | None:
    create_kwargs: dict = {"model_name": model_name, "video_embedding_scope": ["clip", "video"]}
    fh = None
    if video_file is not None:
        if isinstance(video_file, str):
            fh = open(video_file, "rb")
            create_kwargs["video_file"] = (os.path.basename(video_file), fh, "video/mp4")
        else:
            create_kwargs["video_file"] = video_file
    if video_url is not None:
        create_kwargs["video_url"] = video_url
    try:
        task = c.embed.tasks.create(**create_kwargs)
    finally:
        if fh is not None:
            fh.close()
    c.embed.tasks.wait_for_done(task.id, sleep_interval=5.0)
    res = c.embed.tasks.retrieve(task.id)

    ve = getattr(res, "video_embedding", None)
    segments = getattr(ve, "segments", None) or []
    # prefer the whole-video scope vector
    ordered = sorted(
        segments,
        key=lambda s: 0 if getattr(s, "embedding_scope", None) == "video" else 1,
    )
    for seg in ordered:
        for attr in ("embeddings_float", "float_", "float", "embedding"):
            vec = getattr(seg, attr, None)
            if vec:
                return [float(x) for x in vec]
    return None
