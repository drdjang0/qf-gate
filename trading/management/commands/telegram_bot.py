"""Telegram command bot: long-polls Telegram and answers commands from TELEGRAM_CHAT_ID only.

    python manage.py telegram_bot

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment / .env.
"""

from __future__ import annotations

import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from trading.notify import send_telegram
from trading.telegram import handle


class Command(BaseCommand):
    help = "Answer Telegram commands (/status, /profit, /stop, /kill ...) from your own chat."

    def handle(self, *args, **opts):
        token = settings.TRADING["TELEGRAM_BOT_TOKEN"]
        if not (token and settings.TRADING["TELEGRAM_CHAT_ID"]):
            raise CommandError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first (see .env.example).")
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        offset, backoff = None, 1
        self.stdout.write(self.style.SUCCESS("Telegram bot running; send /help from your chat."))
        try:
            while True:
                try:
                    r = requests.get(url, params={"timeout": 50, "offset": offset}, timeout=60)
                    updates = r.json().get("result", []) if r.ok else []
                    backoff = 1
                except requests.RequestException as exc:
                    self.stderr.write(f"Telegram unreachable: {exc}; retrying in {backoff}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                for u in updates:
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    close_old_connections()
                    reply = handle(msg.get("text", ""), (msg.get("chat") or {}).get("id"))
                    if reply:
                        send_telegram(reply)
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")
