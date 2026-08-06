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
    _send(token, chat_id, body,
          buttons=[[{"text": "🎬 different clips", "callback_data": f"rg:{tok}"},
                    {"text": "🅣 tiktok slim", "callback_data": f"fs:{tok}"}]])


def _handle_callback(token: str, cq: dict) -> None:
    """The delivery buttons. "rg:" re-renders the same reference with different footage; "fs:"
    re-renders it in the TikTok slim caption style, keeping the same footage the operator just
    saw (a font swap, not a new cut)."""
    data = (cq.get("data") or "")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    try:                                   # always clear the button's spinner
        _api(token, "answerCallbackQuery", callback_query_id=cq.get("id"),
             text=("re-rendering in tiktok slim…" if data.startswith("fs:")
                   else "rerolling with different clips…"))
    except Exception:  # noqa: BLE001
        pass
    if not (data.startswith("rg:") or data.startswith("fs:")):
        return
    restyle = data.startswith("fs:")
    ctx = _REGEN.get(data[3:])
    if not ctx:
        _send(token, chat_id, "that reroll expired (the bot restarted) — resend the reel link")
        return
    if not os.path.exists(ctx.get("audio") or ""):
        # the reference audio lives in tmp/ and does not survive a redeploy
        _send(token, chat_id, "the reference audio for that one is gone (redeploy) — resend the link")
        return

    def work() -> None:
        from app.reference.intake import recreate_for_profile
        try:
            if restyle:
                # SAME cut, different caption style: reuse the clips this render already used
                res = recreate_for_profile(ctx["pid"], ctx["spans"], ctx["audio"],
                                           font_style="slim",
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
            _send(token, chat_id, body,
                  buttons=[[{"text": "🎬 different clips", "callback_data": f"rg:{nxt}"},
                            {"text": "🅣 tiktok slim", "callback_data": f"fs:{nxt}"}]])
        except Exception as ex:  # noqa: BLE001
            import traceback
            print(f"[tg] reroll failed: {ex}\n{traceback.format_exc()}", flush=True)
            _send(token, chat_id, f"reroll failed: {str(ex)[:300]}")

    threading.Thread(target=work, daemon=True).start()


def _handle(token: str, msg: dict) -> None:
    from app.reference.intake import find_reel_url, process_reel_link
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    url = find_reel_url(text)
    if not url:
        print(f"[tg] message with no reel link: {text[:80]!r}", flush=True)
        _send(token, chat_id,
              "send me an instagram reel link and i'll recreate it for every reference-active "
              "profile (same audio, recreated caption) and drop the results in each profile's "
              "Drive under references/")
        return
    print(f"[tg] reel link received: {url}", flush=True)
    _send(token, chat_id, "on it 🫡")

    def notify(s: str) -> None:
        print(f"[tg] {s.splitlines()[0][:120]}", flush=True)
        _send(token, chat_id, s)

    def work() -> None:
        try:
            results = process_reel_link(
                url, notify,
                on_result=lambda res, spans, audio: _result_message(token, chat_id, res, spans, audio))
            ok = sum(1 for r in results if r.get("ok"))
            if results:
                _send(token, chat_id, f"done — {ok}/{len(results)} recreations in Drive")
        except Exception as ex:  # noqa: BLE001
            import traceback
            print(f"[tg] intake failed: {ex}\n{traceback.format_exc()}", flush=True)
            _send(token, chat_id, f"intake failed: {str(ex)[:300]}")

    threading.Thread(target=work, daemon=True).start()


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
    threading.Thread(target=_loop, args=(token, int(allowed)), daemon=True).start()
    return True
