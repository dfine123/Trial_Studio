"""Telegram intake bot — the operator sends an Instagram reel link, the system recreates it for
every reference-active profile and reports back with Drive links.

Long-polling in a daemon thread (no webhook config, survives Railway's proxy). ONLY the
operator's Telegram user id (settings.telegram_allowed_user_id) is honored; everything else is
ignored silently. Credentials live in Railway env vars — the repo is public, never in code.
"""
from __future__ import annotations

import threading
import time

import httpx


def _api(token: str, method: str, **params):
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=70)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data}")
    return data.get("result")


def _send(token: str, chat_id: int, text: str) -> None:
    try:
        _api(token, "sendMessage", chat_id=chat_id, text=text[:4000],
             disable_web_page_preview=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[tg] send failed: {ex}", flush=True)


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
            results = process_reel_link(url, notify)
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
                           allowed_updates=["message"])
            for u in updates or []:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
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
