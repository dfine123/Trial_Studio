"""REFERENCE INTAKE — recreate an Instagram reel (our caption-over-b-roll format) for every
profile toggled "reference active" in the studio.

Flow per link: download the reel (yt-dlp) → extract its AUDIO (the recreation uses the same
track) → read the burned-in caption text off a frame (Claude vision) → for each reference-active
profile: caption is copied 1:1 (a light personalization pass may make a tiny obvious adjustment —
rare by design) → the standard pipeline renders the reel from THAT profile's clips with the
reference audio → the mp4 uploads to a "references" subfolder in the profile's Drive export
folder. Recreations never enter genlog or the grading queue — they're recreations, not
generations.

Template-style (before/after caption) reels are a later phase; this handles the static-caption
style.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import uuid

IG_URL_RX = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|share|tv)/[^\s]+",
                       re.IGNORECASE)

_WORK = os.path.join("tmp", "reference")


def find_reel_url(text: str) -> str | None:
    m = IG_URL_RX.search(text or "")
    return m.group(0).rstrip(").,") if m else None


def download_reel(url: str) -> str:
    """Download the reel via yt-dlp; returns the local mp4 path."""
    os.makedirs(_WORK, exist_ok=True)
    stem = os.path.join(_WORK, f"ref_{uuid.uuid4().hex[:10]}")
    import yt_dlp
    opts = {
        "outtmpl": stem + ".%(ext)s",
        "format": "mp4/bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 60,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    for ext in ("mp4", "mkv", "webm", "mov"):
        p = f"{stem}.{ext}"
        if os.path.exists(p):
            return p
    raise RuntimeError("download finished but no video file found")


def extract_audio(video_path: str) -> str:
    """The reference's audio track, as m4a — the recreation runs on the same sound."""
    out = os.path.splitext(video_path)[0] + "_audio.m4a"
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "aac", "-b:a", "192k", out],
                   check=True, capture_output=True)
    return out


def _frame_b64(video_path: str, at_sec: float) -> str | None:
    out = os.path.splitext(video_path)[0] + f"_f{int(at_sec * 10)}.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", str(at_sec), "-i", video_path,
                        "-frames:v", "1", "-q:v", "3", out], check=True, capture_output=True)
        with open(out, "rb") as f:
            return base64.standard_b64encode(f.read()).decode()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def extract_caption_text(video_path: str) -> str:
    """Read the caption burned into the reel off two frames (Claude vision). The caption in our
    format is static text over b-roll — two samples guard against an intro frame without text."""
    from anthropic import Anthropic
    from app.config import settings
    frames = [b for b in (_frame_b64(video_path, 1.0), _frame_b64(video_path, 3.0)) if b]
    if not frames:
        raise RuntimeError("could not extract frames from the reference video")
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
               for b in frames]
    content.append({"type": "text", "text":
                    "These frames are from a short-form reel with a text caption overlaid on the "
                    "video. Transcribe the overlay caption text EXACTLY as written — same casing, "
                    "same punctuation, same emoji, preserving line breaks as \\n. Ignore usernames, "
                    "watermarks, UI elements, and any subtitles of spoken audio. Return ONLY JSON: "
                    '{"caption": "the exact text"}'})
    msg = Anthropic(api_key=settings.anthropic_api_key, max_retries=5).messages.create(
        model=settings.caption_model, max_tokens=1000, thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    s, e = text.find("{"), text.rfind("}")
    cap = (json.loads(text[s:e + 1]).get("caption") or "").strip()
    if not cap:
        raise RuntimeError("no caption text found on the reference frames")
    return cap


def reference_active_profiles() -> list[dict]:
    """Profiles toggled 'reference active' in the studio (profile_settings.reference_active)."""
    from app import profiles
    from app.db import SessionLocal
    from app.models import User
    from sqlalchemy import select
    out = []
    with SessionLocal() as s:
        rows = s.scalars(select(User)).all()
        for u in rows:
            try:
                if profiles.profile_settings(u.id).get("reference_active"):
                    out.append({"id": u.id, "name": u.handle or str(u.id)})
            except Exception:  # noqa: BLE001
                continue
    return out


def personalize_caption(caption: str, pid) -> str:
    """Usually a 1:1 copy. A tiny, OBVIOUS personalization (a niche word that maps directly to
    this profile's world) is allowed but rare by design — when in any doubt, the reference text
    ships untouched. Fail-open to the original."""
    try:
        from app import profiles
        from app.caption.llm import complete_json
        persona = profiles.read_persona(pid) or ""
        if not persona.strip():
            return caption
        out = complete_json(
            "You adapt a proven reel caption for a specific creator. DEFAULT: return the caption "
            "EXACTLY as given — character for character. Only if one small word or detail has a "
            "glaringly obvious equivalent in this creator's world may you swap that one detail; "
            "never rewrite, never rephrase, never change the joke. When in ANY doubt, return it "
            "unchanged.\n\nTHE CREATOR:\n" + persona[:1500],
            f"CAPTION:\n{caption}\n\nReturn ONLY JSON: {{\"caption\": \"...\"}}",
            effort="low", max_tokens=600, tag="ref-personalize")
        s, e = out.find("{"), out.rfind("}")
        adapted = (json.loads(out[s:e + 1]).get("caption") or "").strip()
        return adapted or caption
    except Exception:  # noqa: BLE001
        return caption


def recreate_for_profile(pid, caption: str, audio_path: str) -> dict:
    """Render the recreation from THIS profile's clips with the reference audio, then upload to
    the profile's Drive 'references' subfolder. Runs profile-scoped via the request ContextVar so
    the operator's active studio profile is never disturbed."""
    from app import profiles
    from app.drive import export as drive_export
    from app.generate.generator import generate_reel
    token = profiles.set_request_uid(pid)
    try:
        os.makedirs(_WORK, exist_ok=True)
        out_path = os.path.join(_WORK, f"recreation_{uuid.uuid4().hex[:10]}.mp4")
        final_caption = personalize_caption(caption, pid)
        res = generate_reel(
            audio_path=audio_path, niche="", out_path=out_path,
            caption_text=final_caption,
            work_png=os.path.join(_WORK, f"cap_{uuid.uuid4().hex[:8]}.png"),
            # recreations read as ONE scene — same car(s), same setting (operator rule);
            # the variety machinery is for original reels, not recreations
            coherent_clips=True,
        )
        stem = "ref_" + time.strftime("%Y%m%d_%H%M%S")
        up = drive_export.upload_reference(pid, out_path, stem)
        return {"ok": True, "caption": final_caption, "link": up.get("link"),
                "duration": res.get("duration")}
    finally:
        profiles.reset_request_uid(token)


def process_reel_link(url: str, notify=lambda s: None) -> list[dict]:
    """The full intake: download → audio + caption → recreate for every reference-active profile.
    notify(text) receives human-readable progress (the Telegram bot forwards it)."""
    targets = reference_active_profiles()
    if not targets:
        notify("no profiles are toggled 'reference active' in the studio — nothing to recreate")
        return []
    notify(f"downloading the reference…")
    video = download_reel(url)
    audio = extract_audio(video)
    caption = extract_caption_text(video)
    notify(f'caption read off the reference:\n"{caption}"\n\nrecreating for '
           f"{len(targets)} profile(s): " + ", ".join(t["name"] for t in targets))
    results = []
    for t in targets:
        try:
            r = recreate_for_profile(t["id"], caption, audio)
            results.append({"profile": t["name"], **r})
            notify(f"✅ {t['name']} — done" + (f"\n{r['link']}" if r.get("link") else ""))
        except Exception as ex:  # noqa: BLE001
            results.append({"profile": t["name"], "ok": False, "error": str(ex)[:200]})
            notify(f"❌ {t['name']} — {str(ex)[:160]}")
    try:
        os.remove(video)
    except OSError:
        pass
    return results
