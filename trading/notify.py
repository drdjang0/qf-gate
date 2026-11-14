"""Alerts: Telegram message + JSON webhooks (ideas from freqtrade's Telegram and webhook support).

notify(event, text, data) never raises and never blocks the caller: messages are sent from a
background thread, webhooks are retried. With no TELEGRAM_* / WEBHOOK_URLS configured it only logs.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import requests
from django.conf import settings

log = logging.getLogger("trading.notify")
RETRIES = 3

EVENTS = {
    "bot.start": "Bot started", "bot.stop": "Bot stopped", "bot.fill": "Order filled",
    "bot.blocked": "Order blocked", "bot.error": "Bot error", "control.halt": "Trading halted",
    "control.resume": "Trading resumed", "service.down": "Service down", "test": "Test",
}


def _cfg():
    return settings.TRADING


def send_telegram(text: str, chat_id: str | None = None, session=None) -> bool:
    token, chat = _cfg()["TELEGRAM_BOT_TOKEN"], chat_id or _cfg()["TELEGRAM_CHAT_ID"]
    if not (token and chat):
        return False
    http = session or requests
    for attempt in range(RETRIES):
        try:
            r = http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=10)
            if r.ok:
                return True
            log.warning("Telegram send failed: HTTP %s", r.status_code)
        except requests.RequestException as exc:
            log.warning("Telegram send failed: %s", exc)
        time.sleep(2**attempt)
    return False


def send_webhooks(payload: dict, session=None) -> int:
    http = session or requests
    ok = 0
    for url in _cfg()["WEBHOOK_URLS"]:
        for attempt in range(RETRIES):
            try:
                r = http.post(url, json=payload, timeout=10)
                if r.ok:
                    ok += 1
                    break
            except requests.RequestException as exc:
                log.warning("Webhook %s failed: %s", url, exc)
            time.sleep(2**attempt)
    return ok


def configured() -> dict:
    c = _cfg()
    return {"telegram": bool(c["TELEGRAM_BOT_TOKEN"] and c["TELEGRAM_CHAT_ID"]), "webhooks": len(c["WEBHOOK_URLS"])}


def notify(event: str, text: str, data: dict | None = None, background: bool = True) -> None:
    title = EVENTS.get(event, event)
    msg = f"QF-gate · {title}\n{text}"
    log.info("%s: %s", event, text)
    payload = {"event": event, "title": title, "text": text, "data": data or {},
               "time": datetime.now(timezone.utc).isoformat()}

    def _send():
        try:
            send_telegram(msg)
            send_webhooks(payload)
        except Exception:  # noqa: BLE001 - alerts must never break trading
            log.exception("Alert delivery failed")

    if not any(configured().values()):
        return
    if background:
        threading.Thread(target=_send, daemon=True, name=f"notify-{event}").start()
    else:
        _send()
