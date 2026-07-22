"""REFERENCE INTAKE — recreate an Instagram reel (our caption-over-b-roll format) for every
profile toggled "reference active" in the studio.

Flow per link: download the reel (yt-dlp) → extract its AUDIO (the recreation uses the same
track) → read the burned-in caption text off a frame (Claude vision) → for each reference-active
profile: caption is copied 1:1 (a light personalization pass may make a tiny obvious adjustment —
rare by design) → the standard pipeline renders the reel from THAT profile's clips with the
reference audio → the mp4 uploads to a "references" subfolder in the profile's Drive export
folder. Recreations never enter genlog or the grading queue — they're recreations, not
generations.

STATIC (one caption span): a coherent single-scene reel, 5–9s. DYNAMIC (the caption changes
partway — setup → payoff): the recreation follows the REFERENCE's own timeline — caption parts
switch at the reference's times (on a cut), clips re-match per part (struggle clips under the
setup, flexes under the payoff), and the reel runs the reference's full length.
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


def _download_via_apify(url: str) -> str | None:
    """Primary path: Apify's Instagram scraper — no IG login/cookies needed. Returns the local
    mp4 path, or None to fall through to yt-dlp (missing token, actor error, no video URL)."""
    from app.config import settings
    token = (getattr(settings, "apify_token", "") or os.environ.get("APIFY_TOKEN")
             or os.environ.get("APIFY_API_TOKEN") or "").strip()
    if not token:
        return None
    import requests
    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items",
            params={"token": token},
            json={"directUrls": [url], "resultsType": "posts", "resultsLimit": 1,
                  "addParentData": False},
            timeout=180,
        )
        r.raise_for_status()
        items = r.json() or []
        video_url = next((it.get("videoUrl") for it in items if it.get("videoUrl")), None)
        if not video_url:
            print(f"[intake] apify returned no videoUrl for {url}", flush=True)
            return None
        os.makedirs(_WORK, exist_ok=True)
        path = os.path.join(_WORK, f"ref_{uuid.uuid4().hex[:10]}.mp4")
        with requests.get(video_url, stream=True, timeout=180) as dl:
            dl.raise_for_status()
            with open(path, "wb") as f:
                for chunk in dl.iter_content(1 << 16):
                    f.write(chunk)
        return path
    except Exception as ex:  # noqa: BLE001 — apify is best-effort; yt-dlp is the fallback
        print(f"[intake] apify path failed ({str(ex)[:120]}) — falling back to yt-dlp", flush=True)
        return None


def download_reel(url: str) -> str:
    """Download the reel — Apify scraper first (no IG login), yt-dlp + cookies as fallback."""
    p = _download_via_apify(url)
    if p:
        return _validated(p)
    os.makedirs(_WORK, exist_ok=True)
    stem = os.path.join(_WORK, f"ref_{uuid.uuid4().hex[:10]}")
    import yt_dlp
    opts = {
        "outtmpl": stem + ".%(ext)s",
        "format": "bestvideo*+bestaudio/best/mp4",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 60,
    }
    # Instagram requires login cookies for media since mid-2026 ("empty media response" logged
    # out). The operator uploads their browser's instagram.com cookies once (app: Reference
    # settings, or POST /api/reference/cookies); we pass them to yt-dlp when present.
    cookies = os.path.join("var", "ig_cookies.txt")
    if os.path.exists(cookies):
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as ex:  # noqa: BLE001 — surface the real cause, hint the cookie fix
        msg = str(ex)
        if "empty media response" in msg or "login" in msg.lower() or "cookies" in msg.lower():
            raise RuntimeError(
                "Instagram now requires login cookies for downloads. Upload your instagram.com "
                "cookies once in the app (Generate rail → IG cookies) and resend the link."
                + (" (Cookies are uploaded but seem expired — re-export and upload fresh ones.)"
                   if os.path.exists(cookies) else "")) from ex
        raise
    path = None
    for ext in ("mp4", "mkv", "webm", "mov"):
        p = f"{stem}.{ext}"
        if os.path.exists(p):
            path = p
            break
    if not path:
        raise RuntimeError("download finished but no video file found")
    return _validated(path)


def _validated(path: str) -> str:
    """ffprobe the download BEFORE ffmpeg so failures are readable, not exit-code soup."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                            "-of", "csv=p=0", path], capture_output=True, text=True)
    streams = set((probe.stdout or "").split())
    if probe.returncode != 0 or "video" not in streams:
        raise RuntimeError("the downloaded file isn't playable video (Instagram likely served an "
                           "error page — check the Apify token / IG cookies)")
    if "audio" not in streams:
        raise RuntimeError("the reel downloaded without its audio track (Instagram is serving "
                           "video-only — check the Apify token / IG cookies)")
    return path


def extract_audio(video_path: str) -> str:
    """The reference's audio track, as m4a — the recreation runs on the same sound."""
    out = os.path.splitext(video_path)[0] + "_audio.m4a"
    r = subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "aac", "-b:a", "192k", out],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["unknown ffmpeg error"]
        raise RuntimeError(f"audio extract failed: {tail[0][:160]}")
    return out


def _frame_b64(video_path: str, at_sec: float, scale: str | None = None) -> str | None:
    out = os.path.splitext(video_path)[0] + f"_f{int(at_sec * 10)}.jpg"
    try:
        vf = ["-vf", f"scale={scale}"] if scale else []
        subprocess.run(["ffmpeg", "-y", "-ss", str(at_sec), "-i", video_path,
                        "-frames:v", "1", *vf, "-q:v", "3", out], check=True, capture_output=True)
        with open(out, "rb") as f:
            return base64.standard_b64encode(f.read()).decode()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def video_duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], check=True, capture_output=True,
                         text=True)
    return float(out.stdout.strip())


def _transcribe_frames(video_path: str, times: list[float]) -> list[str]:
    """Per-frame caption transcription (Claude vision, ONE call over all sampled frames).
    Returns one string per timestamp — empty when that frame shows no overlay caption."""
    from anthropic import Anthropic
    from app.config import settings
    content = []
    kept: list[int] = []
    for i, t in enumerate(times):
        b = _frame_b64(video_path, t, scale="360:-2")
        if b:
            kept.append(i)
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": b}})
    if not content:
        raise RuntimeError("could not extract frames from the reference video")
    content.append({"type": "text", "text":
                    f"These {len(kept)} frames are consecutive time samples from ONE short-form "
                    "reel whose text caption is overlaid on the video and may CHANGE partway "
                    "through. For EACH frame, in order, transcribe the overlay caption text "
                    "EXACTLY as written — same casing, same punctuation, same emoji, line breaks "
                    'as \\n; use "" when a frame has no overlay caption. The same on-screen text '
                    "must be transcribed IDENTICALLY on every frame it appears in. Ignore "
                    "usernames, watermarks, UI elements, and subtitles of spoken audio. Return "
                    'ONLY JSON: {"captions": ["frame 1 text", "frame 2 text", ...]} with exactly '
                    f"{len(kept)} entries."})
    msg = Anthropic(api_key=settings.anthropic_api_key, max_retries=5).messages.create(
        model=settings.caption_model, max_tokens=4000, thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    s, e = text.find("{"), text.rfind("}")
    caps = json.loads(text[s:e + 1]).get("captions") or []
    caps = [str(c or "").strip() for c in caps]
    # re-align to the requested timestamps (frames that failed to extract read as empty)
    out = [""] * len(times)
    for j, i in enumerate(kept):
        if j < len(caps):
            out[i] = caps[j]
    return out


def _norm_cap(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def _closest(text: str, a: str, b: str) -> str | None:
    """Classify a transcription as caption A or B (fuzzy — dense-pass reads vary slightly)."""
    import difflib
    t, na, nb = _norm_cap(text), _norm_cap(a), _norm_cap(b)
    if not t:
        return None
    ra = difflib.SequenceMatcher(None, t, na).ratio()
    rb = difflib.SequenceMatcher(None, t, nb).ratio()
    best, r = ("a", ra) if ra >= rb else ("b", rb)
    return best if r >= 0.6 else None


def _refine_boundary(video_path: str, t_lo: float, t_hi: float,
                     text_a: str, text_b: str) -> float | None:
    """Pin a caption transition to ~±0.05s: sample every 0.1s inside the coarse bracket and find
    the first frame showing caption B. The switch must align with the REFERENCE exactly — a
    half-second-late caption flip reads as sloppy. Fail-open (None keeps the coarse boundary)."""
    try:
        times, t = [], max(0.0, t_lo)
        while t <= t_hi + 0.001 and len(times) < 16:
            times.append(round(t, 3))
            t += 0.1
        if len(times) < 2:
            return None
        caps = _transcribe_frames(video_path, times)
        last_a, first_b = None, None
        for t0, c in zip(times, caps):
            cls = _closest(c, text_a, text_b)
            if cls == "a":
                last_a = t0
            elif cls == "b":
                first_b = t0
                break
        if first_b is None:
            return None
        lo = last_a if last_a is not None else max(0.0, first_b - 0.1)
        return round((lo + first_b) / 2.0, 3)
    except Exception:  # noqa: BLE001
        return None


def extract_caption_timeline(video_path: str, duration: float) -> list[dict]:
    """The reference's caption TIMELINE: [{text, start, end}] in order. A static reel yields one
    span; a dynamic reel (the caption changes partway — e.g. setup → payoff) yields several.
    Frames are sampled ~every 0.5s (wider on long refs), transcribed in one vision call, and
    grouped in code: adjacent same-text samples form a span, boundaries land at the midpoint
    between differing samples, sub-0.8s flickers merge into the previous span."""
    step = max(0.5, duration / 44.0)
    times, t = [], 0.25
    while t < duration - 0.1:
        times.append(round(t, 3))
        t += step
    if not times:
        times = [max(0.0, duration / 2.0)]
    caps = _transcribe_frames(video_path, times)

    spans: list[dict] = []
    for t0, c in zip(times, caps):
        if not c.strip():
            continue
        if spans and _norm_cap(spans[-1]["text"]) == _norm_cap(c):
            spans[-1]["_last_t"] = t0
            continue
        spans.append({"text": c.strip(), "_first_t": t0, "_last_t": t0})
    if not spans:
        raise RuntimeError("no caption text found on the reference frames")

    # timing: a span starts at the midpoint between it and the previous DIFFERENT sample
    for i, sp in enumerate(spans):
        if i == 0:
            sp["start"] = 0.0
        else:
            sp["start"] = round((spans[i - 1]["_last_t"] + sp["_first_t"]) / 2.0, 3)
        sp["end"] = duration
    for i in range(len(spans) - 1):
        spans[i]["end"] = spans[i + 1]["start"]

    # transcription-flicker guard: a sub-0.8s span is noise — merge it into its neighbor
    merged: list[dict] = []
    for sp in spans:
        if merged and (sp["end"] - sp["start"]) < 0.8:
            merged[-1]["end"] = sp["end"]
            continue
        if merged and _norm_cap(merged[-1]["text"]) == _norm_cap(sp["text"]):
            merged[-1]["end"] = sp["end"]
            continue
        merged.append(sp)
    for sp in merged:
        sp.pop("_first_t", None)
        sp.pop("_last_t", None)

    # PRECISION PASS — the coarse boundary sits between two samples a full step apart; a dense
    # 0.1s pass inside that bracket pins each transition to the reference's actual switch frame
    for i in range(1, len(merged)):
        b = merged[i]["start"]
        refined = _refine_boundary(video_path, b - step / 2.0 - 0.05, b + step / 2.0 + 0.05,
                                   merged[i - 1]["text"], merged[i]["text"])
        if refined is not None and 0.2 < refined < duration - 0.2:
            merged[i]["start"] = refined
            merged[i - 1]["end"] = refined
    return merged


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
    return personalize_caption_parts([caption], pid)[0]


def personalize_caption_parts(parts: list[str], pid) -> list[str]:
    """The multi-part variant — the parts form ONE joke arc (setup → payoff), so they're adapted
    as a unit under the same DEFAULT-verbatim rule. Fail-open to the originals."""
    try:
        from app import profiles
        from app.caption.llm import complete_json
        persona = profiles.read_persona(pid) or ""
        if not persona.strip():
            return list(parts)
        listing = json.dumps(parts, ensure_ascii=False)
        out = complete_json(
            "You adapt a proven reel caption for a specific creator. The caption may arrive in "
            "several PARTS shown in sequence (one joke arc — e.g. setup then payoff). DEFAULT: "
            "return every part EXACTLY as given — character for character. Only if one small word "
            "or detail has a glaringly obvious equivalent in this creator's world may you swap "
            "that one detail; never rewrite, never rephrase, never change the joke, never change "
            "how many parts there are. When in ANY doubt, return them unchanged.\n\n"
            "THE CREATOR:\n" + persona[:1500],
            f"CAPTION PARTS (in order):\n{listing}\n\n"
            "Return ONLY JSON: {\"parts\": [\"...\", ...]} with the same number of parts.",
            effort="low", max_tokens=800, tag="ref-personalize")
        s, e = out.find("{"), out.rfind("}")
        adapted = json.loads(out[s:e + 1]).get("parts") or []
        adapted = [str(a or "").strip() for a in adapted]
        if len(adapted) == len(parts) and all(adapted):
            return adapted
        return list(parts)
    except Exception:  # noqa: BLE001
        return list(parts)


def recreate_for_profile(pid, spans: list[dict], audio_path: str) -> dict:
    """Render the recreation from THIS profile's clips with the reference audio, then upload to
    the profile's Drive 'references' subfolder. Runs profile-scoped via the request ContextVar so
    the operator's active studio profile is never disturbed.

    One span = static recreation (coherent single-caption reel). Several spans = DYNAMIC: the
    caption changes at the reference's own times, clips re-match per span (setup clips vs payoff
    clips), the reel runs the reference's full length."""
    from app import profiles
    from app.drive import export as drive_export
    from app.generate.generator import generate_dynamic_reel, generate_reel
    token = profiles.set_request_uid(pid)
    try:
        os.makedirs(_WORK, exist_ok=True)
        out_path = os.path.join(_WORK, f"recreation_{uuid.uuid4().hex[:10]}.mp4")
        parts = personalize_caption_parts([sp["text"] for sp in spans], pid)
        if len(spans) == 1:
            res = generate_reel(
                audio_path=audio_path, niche="", out_path=out_path,
                caption_text=parts[0],
                work_png=os.path.join(_WORK, f"cap_{uuid.uuid4().hex[:8]}.png"),
                # recreations read as ONE scene — same car(s), same setting (operator rule);
                # the variety machinery is for original reels, not recreations
                coherent_clips=True,
            )
            final_caption = parts[0]
        else:
            final_spans = [{**sp, "text": p} for sp, p in zip(spans, parts)]
            res = generate_dynamic_reel(
                audio_path=audio_path, spans=final_spans, out_path=out_path,
                work_dir=_WORK,
            )
            final_caption = "  /  ".join(parts)
        stem = "ref_" + time.strftime("%Y%m%d_%H%M%S")
        up = drive_export.upload_reference(pid, out_path, stem)
        return {"ok": True, "caption": final_caption, "link": up.get("link"),
                "duration": res.get("duration"), "clips": res.get("clips"),
                "spans": res.get("spans")}
    finally:
        profiles.reset_request_uid(token)


def process_reel_link(url: str, notify=lambda s: None) -> list[dict]:
    """The full intake: download → audio + caption → recreate for every reference-active profile.
    notify(text) receives human-readable progress (the Telegram bot forwards it). Every stage
    ALSO prints to stdout — the debug endpoint's HTTP response dies at the Railway edge on long
    runs, so the logs are the only reliable observability for those."""
    _notify = notify

    def notify(s: str) -> None:
        print(f"[ref] {s.splitlines()[0][:140]}", flush=True)
        _notify(s)

    targets = reference_active_profiles()
    if not targets:
        notify("no profiles are toggled 'reference active' in the studio — nothing to recreate")
        return []
    notify(f"downloading the reference…")
    video = download_reel(url)
    audio = extract_audio(video)
    spans = extract_caption_timeline(video, video_duration(video))
    if len(spans) == 1:
        cap_desc = f'caption read off the reference:\n"{spans[0]["text"]}"'
    else:
        arc = "\n".join(f'{i + 1}. [{sp["start"]:.1f}s–{sp["end"]:.1f}s] "{sp["text"]}"'
                        for i, sp in enumerate(spans))
        cap_desc = f"dynamic caption — {len(spans)} parts read off the reference:\n{arc}"
    notify(f"{cap_desc}\n\nrecreating for "
           f"{len(targets)} profile(s): " + ", ".join(t["name"] for t in targets))
    results = []
    for t in targets:
        try:
            r = recreate_for_profile(t["id"], spans, audio)
            results.append({"profile": t["name"], **r})
            for c in (r.get("clips") or []):
                print(f"[ref]   clip: {(c.get('summary') or '')[:110]}", flush=True)
            notify(f"✅ {t['name']} — done" + (f"\n{r['link']}" if r.get("link") else ""))
        except Exception as ex:  # noqa: BLE001
            results.append({"profile": t["name"], "ok": False, "error": str(ex)[:200]})
            notify(f"❌ {t['name']} — {str(ex)[:160]}")
    try:
        os.remove(video)
    except OSError:
        pass
    return results
