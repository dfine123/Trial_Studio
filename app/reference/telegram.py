"""Telegram intake bot — the operator sends an Instagram reel link, the system recreates it for
every reference-active profile and reports back with Drive links.

Long-polling in a daemon thread (no webhook config, survives Railway's proxy). ONLY the
operator's Telegram user id (settings.telegram_allowed_user_id) is honored; everything else is
ignored silently. Credentials live in Railway env vars — the repo is public, never in code.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import httpx


def _api(token: str, method: str, **params):
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=70)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data}")
    return data.get("result")


def _send(token: str, chat_id: int, text: str, buttons: list | None = None) -> None:
    try:
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        _api(token, "sendMessage", **params)
    except Exception as ex:  # noqa: BLE001
        print(f"[tg] send failed: {ex}", flush=True)


# REROLL STATE (2026-07-22): each delivered recreation gets a button that re-renders the SAME
# reference with different footage. Telegram callback_data is capped at 64 bytes, so the button
# carries a short token and the context lives here. In-memory by design — a bot restart just
# means the operator resends the link; nothing is lost but the shortcut.
_REGEN: dict[str, dict] = {}
_REGEN_MAX = 200


def _remember(ctx: dict) -> str:
    tok = uuid.uuid4().hex[:10]
    _REGEN[tok] = ctx
    while len(_REGEN) > _REGEN_MAX:      # bounded: drop the oldest shortcut
        _REGEN.pop(next(iter(_REGEN)), None)
    return tok


def _result_message(token: str, chat_id: int, res: dict, spans: list, audio: str) -> None:
    """One delivery message per profile, with its own reroll button."""
    name = res.get("profile", "?")
    if not res.get("ok"):
        _send(token, chat_id, f"❌ {name} — {str(res.get('error'))[:160]}")
        return
    used = [c.get("clip_id") for c in (res.get("clips") or []) if c.get("clip_id")]
    tok = _remember({"pid": res.get("pid"), "name": name, "spans": spans, "audio": audio,
                     "used": list(used), "chat_id": chat_id})
    body = f"✅ {name} — done" + (f"\n{res['link']}" if res.get("link") else "")
    if res.get("window_left") is not None:
        body += f"\n· today's clips: {res['window_left']} left"
    _send(token, chat_id, body,
          buttons=[[{"text": "🎬 different clips", "callback_data": f"rg:{tok}"},
                    {"text": "🅣 tiktok slim", "callback_data": f"fs:{tok}"}]])


# ── THE QUEUE (2026-11-10) ───────────────────────────────────────────────────────────────
# Every link, reroll and restyle used to start its own thread the moment it arrived, so a reel
# sent while another was still rendering ran ALONGSIDE it — and the operator kept getting
# errors back. Now there is exactly one worker: jobs line up and run one at a time, in the order
# they were sent, and each one tells you where it sits in line.
#
# Link jobs are written to the volume as they are queued and cleared as they finish, so a
# deploy or restart picks the line back up instead of silently dropping what was waiting.
# Rerolls/restyles aren't persisted: they lean on the reference audio in tmp/, which a restart
# wipes anyway (the button already says so when that happens).
import json as _json
import queue as _queue

_Q: "_queue.Queue[dict]" = _queue.Queue()
_QUEUE_PATH = os.path.join("var", "copycat_queue.json")
_PENDING: list[dict] = []          # persisted mirror of the link jobs not yet finished
_PENDING_LOCK = threading.Lock()
_BUSY = {"job": None}


def _save_pending() -> None:
    try:
        os.makedirs(os.path.dirname(_QUEUE_PATH), exist_ok=True)
        tmp = _QUEUE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(_PENDING, fh)
        os.replace(tmp, _QUEUE_PATH)
    except OSError as ex:
        print(f"[tg] queue persist failed: {ex}", flush=True)


def _ahead() -> int:
    """How many jobs will run before one queued right now."""
    return _Q.qsize() + (1 if _BUSY["job"] is not None else 0)


def _enqueue(job: dict) -> int:
    """Queue a job; returns its place in line (1 = runs next/now)."""
    place = _ahead() + 1
    if job["kind"] == "link":
        job.setdefault("id", uuid.uuid4().hex[:10])
        with _PENDING_LOCK:
            _PENDING.append({k: job[k] for k in ("id", "kind", "url", "chat_id")})
            _save_pending()
    _Q.put(job)
    return place


def _done(job: dict) -> None:
    if job["kind"] != "link":
        return
    with _PENDING_LOCK:
        _PENDING[:] = [j for j in _PENDING if j.get("id") != job.get("id")]
        _save_pending()


def _run_link(token: str, job: dict) -> None:
    from app.reference.intake import process_reel_link
    chat_id, url = job["chat_id"], job["url"]

    def notify(s: str) -> None:
        print(f"[tg] {s.splitlines()[0][:120]}", flush=True)
        _send(token, chat_id, s)

    try:
        results = process_reel_link(
            url, notify,
            on_result=lambda res, spans, audio: _result_message(token, chat_id, res, spans, audio))
        ok = sum(1 for r in results if r.get("ok"))
        if results:
            _send(token, chat_id, f"done — {ok}/{len(results)} recreations in Drive")
    except Exception as ex:  # noqa: BLE001 — one bad link must not stop the line
        import traceback
        print(f"[tg] intake failed: {ex}\n{traceback.format_exc()}", flush=True)
        _send(token, chat_id, f"intake failed: {str(ex)[:300]}")


def _run_regen(token: str, job: dict) -> None:
    """A button press: "rg" re-renders the same reference with different footage; "fs" re-renders
    it in the TikTok slim caption style, keeping the footage the operator just saw."""
    from app.reference.intake import recreate_for_profile
    ctx, chat_id, restyle = job["ctx"], job["chat_id"], job["kind"] == "restyle"
    if not os.path.exists(ctx.get("audio") or ""):
        # the reference audio lives in tmp/ and does not survive a redeploy
        _send(token, chat_id, "the reference audio for that one is gone (redeploy) — resend the link")
        return
    try:
        if restyle:
            # SAME cut, different caption style: reuse the clips this render already used
            res = recreate_for_profile(ctx["pid"], ctx["spans"], ctx["audio"], font_style="slim",
                                       only_clip_ids=ctx.get("used") or None)
            nxt = _remember(ctx)
            body = f"🅣 {ctx['name']} — tiktok slim"
        else:
            res = recreate_for_profile(ctx["pid"], ctx["spans"], ctx["audio"],
                                       exclude_clip_ids=ctx["used"])
            used = [c.get("clip_id") for c in (res.get("clips") or []) if c.get("clip_id")]
            # exclude everything seen so far, so each reroll keeps finding new footage
            nxt = _remember({**ctx, "used": list({*ctx["used"], *used})})
            body = f"🎬 {ctx['name']} — new clips"
        body += (f"\n{res['link']}" if res.get("link") else "")
        if res.get("window_left") is not None:
            body += f"\n· today's clips: {res['window_left']} left"
        _send(token, chat_id, body,
              buttons=[[{"text": "🎬 different clips", "callback_data": f"rg:{nxt}"},
                        {"text": "🅣 tiktok slim", "callback_data": f"fs:{nxt}"}]])
    except Exception as ex:  # noqa: BLE001
        import traceback
        print(f"[tg] reroll failed: {ex}\n{traceback.format_exc()}", flush=True)
        _send(token, chat_id, f"reroll failed: {str(ex)[:300]}")


def _worker(token: str) -> None:
    """The ONE consumer. Holds the intake run lock per job, so an operator backfill (which runs
    outside the bot) also waits its turn instead of overlapping a CopyCat render."""
    from app.reference.intake import RUN_LOCK
    print("[tg] copycat queue worker up", flush=True)
    while True:
        job = _Q.get()
        _BUSY["job"] = job
        try:
            with RUN_LOCK:
                if job["kind"] == "link":
                    _run_link(token, job)
                else:
                    _run_regen(token, job)
        except Exception as ex:  # noqa: BLE001 — the worker must never die
            print(f"[tg] worker error: {ex}", flush=True)
        finally:
            _BUSY["job"] = None
            _done(job)
            _Q.task_done()


def _resume(token: str) -> None:
    """Re-queue link jobs a restart interrupted, and tell the operator."""
    try:
        with open(_QUEUE_PATH, encoding="utf-8") as fh:
            saved = _json.load(fh) or []
    except (OSError, ValueError):
        saved = []
    if not saved:
        return
    with _PENDING_LOCK:
        _PENDING[:] = []
    for j in saved:
        _enqueue({"kind": "link", "id": j.get("id"), "url": j["url"], "chat_id": j["chat_id"]})
    chat = saved[0]["chat_id"]
    _send(token, chat, f"↻ back up after a restart — resuming {len(saved)} queued "
                       f"reel{'s' if len(saved) != 1 else ''}")


def _handle_callback(token: str, cq: dict) -> None:
    data = (cq.get("data") or "")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    if not (data.startswith("rg:") or data.startswith("fs:")):
        return
    restyle = data.startswith("fs:")
    ctx = _REGEN.get(data[3:])
    place = _ahead() + 1
    try:                                   # always clear the button's spinner
        what = "tiktok slim" if restyle else "different clips"
        _api(token, "answerCallbackQuery", callback_query_id=cq.get("id"),
             text=(f"{what} — starting" if place == 1 else f"{what} — queued, #{place} in line"))
    except Exception:  # noqa: BLE001
        pass
    if not ctx:
        _send(token, chat_id, "that reroll expired (the bot restarted) — resend the reel link")
        return
    _enqueue({"kind": "restyle" if restyle else "reroll", "ctx": ctx, "chat_id": chat_id})


def _handle(token: str, msg: dict) -> None:
    from app.reference.intake import find_reel_urls
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    urls = find_reel_urls(text)
    if not urls:
        print(f"[tg] message with no reel link: {text[:80]!r}", flush=True)
        _send(token, chat_id,
              "send me an instagram reel link and i'll recreate it for every reference-active "
              "profile (same audio, recreated caption) and drop the results in each profile's "
              "Drive under references/")
        return
    places = []
    for u in urls:
        print(f"[tg] reel link received: {u}", flush=True)
        places.append(_enqueue({"kind": "link", "url": u, "chat_id": chat_id}))
    if len(urls) == 1:
        _send(token, chat_id, "on it 🫡" if places[0] == 1 else f"queued 🫡 — #{places[0]} in line")
    else:
        _send(token, chat_id, f"queued 🫡 — {len(urls)} reels, #{places[0]}–#{places[-1]} in line")


def _loop(token: str, allowed_id: int) -> None:
    print("[tg] reference bot polling", flush=True)
    offset = 0
    while True:
        try:
            updates = _api(token, "getUpdates", offset=offset, timeout=50,
                           allowed_updates=["message", "callback_query"])
            for u in updates or []:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                cq = u.get("callback_query")
                if cq:
                    if ((cq.get("from") or {}).get("id")) == allowed_id:
                        _handle_callback(token, cq)
                    continue
                msg = u.get("message") or {}
                if ((msg.get("from") or {}).get("id")) != allowed_id:
                    # operator-only: no reply, but log the id so a mis-set
                    # TELEGRAM_ALLOWED_USER_ID is diagnosable from Railway logs
                    print(f"[tg] ignored message from user "
                          f"{(msg.get('from') or {}).get('id')}", flush=True)
                    continue
                _handle(token, msg)
        except Exception as ex:  # noqa: BLE001
            print(f"[tg] poll error: {ex}", flush=True)
            time.sleep(10)


def start_bot_if_configured() -> bool:
    """Called at app startup. No-op unless TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_ID are set."""
    from app.config import settings
    token = (getattr(settings, "telegram_bot_token", "") or "").strip()
    allowed = (getattr(settings, "telegram_allowed_user_id", "") or "").strip()
    if not token or not allowed:
        return False
    if getattr(settings, "demo_mode", False):
        return False   # the demo service must never run the operator bot
    threading.Thread(target=_worker, args=(token,), daemon=True).start()
    _resume(token)
    threading.Thread(target=_loop, args=(token, int(allowed)), daemon=True).start()
    return True
