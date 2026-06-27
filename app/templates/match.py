"""Match a creator's indexed clips to a template's segments by the author's free-text clip-type.

Judges fit from the EXISTING indexing (summary/setting/vibe) — no new field, no re-index (per the
user). Honors the author's fallbacks ("can audible to X if not there"). If a segment genuinely
can't be filled, returns ok=false so instantiation aborts with a clear message (the user's call).
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You assign a creator's CLIPS to the SEGMENTS of a short-form video template. Each segment wants a certain KIND of clip — described in the author's own words, which MAY include a fallback ("can audible to X if not there"). For EACH segment, pick the single best-fitting clip_id from the creator's library, honoring the fallback when the ideal kind isn't present. Each clip is used at most ONCE (don't reuse a clip across segments unless there is genuinely no alternative). Judge fit from each clip's summary / setting / vibe. If a segment has NO acceptable clip even with its fallback, set ok=false and name it.

Return ONLY JSON, no prose: {"assignments": {"<segment_index>": "<clip_id>"}, "ok": true|false, "warning": "<what can't be filled, or null>"}"""


def match_clips(segments: list[dict], clips: list[dict]) -> dict:
    """segments: [{index, clip_type}]; clips: [{id, summary, setting, vibe}].
    Returns {assignments: {seg_index(str): clip_id}, ok: bool, warning: str|None}."""
    if not clips:
        return {"assignments": {}, "ok": False, "warning": "this creator has no indexed clips"}
    seg_lines = [f"Segment {s['index']}: wants — {s.get('clip_type') or 'any clip'}" for s in segments]
    clip_lines = [
        f"[{c['id']}] {(c.get('summary') or '').strip()[:150]} | setting: {c.get('setting') or '?'} "
        f"| vibe: {', '.join((c.get('vibe') or [])[:6])}"
        for c in clips
    ]
    user = "SEGMENTS:\n" + "\n".join(seg_lines) + "\n\nCREATOR CLIPS:\n" + "\n".join(clip_lines)
    out = complete_json(_SYS, user, effort="medium", max_tokens=900)
    start, end = out.find("{"), out.rfind("}")
    if start == -1:
        return {"assignments": {}, "ok": False, "warning": "match failed"}
    try:
        d = json.loads(out[start:end + 1])
        d.setdefault("assignments", {})
        d.setdefault("ok", bool(d["assignments"]))
        return d
    except json.JSONDecodeError:
        return {"assignments": {}, "ok": False, "warning": "match failed"}
