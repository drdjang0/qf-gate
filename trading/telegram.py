"""Telegram commands (idea from freqtrade's Telegram control). Only TELEGRAM_CHAT_ID is obeyed.

handle(text, chat_id) is a plain function (easy to test); `manage.py telegram_bot` long-polls
Telegram and replies with its result.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone as dj_tz

from . import bots
from .models import Bot, BotOrder, TradingControl

HELP = """Commands
/status - bots, positions, kill switch
/profit - realised P&L per bot
/daily [n] - realised P&L for the last n days (default 7)
/balance - cash and position per running bot
/stop - halt new entries (positions still managed)
/start - allow entries again
/kill confirm - flatten every position and stop all bots
/forceexit <bot id> - sell one bot's position now
/help - this list"""


def _bots():
    return Bot.objects.filter(status=Bot.Status.RUNNING).select_related("config__coin")


def handle(text: str, chat_id: str) -> str | None:
    if not settings.TRADING["TELEGRAM_CHAT_ID"] or str(chat_id) != str(settings.TRADING["TELEGRAM_CHAT_ID"]):
        return None  # ignore everyone else, silently
    parts = (text or "").strip().split()
    if not parts:
        return None
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    c = TradingControl.get()
    if cmd == "/help":
        return HELP
    if cmd == "/status":
        lines = [f"Entries: {'HALTED - ' + c.halted_reason if c.halted else 'allowed'}"]
        for b in _bots():
            p = b.pos
            pos = f"long {p['qty']:.6g} @ {p['avg']:,.6g}, stop {p['stop']:,.6g}" if p else "flat"
            lines.append(f"#{b.pk} {b.config.name} [{'LIVE' if b.mode == 'live' else 'dry-run'}] {b.config.coin.pair}: {pos}")
        return "\n".join(lines) if len(lines) > 1 else lines[0] + "\nNo bots running."
    if cmd == "/profit":
        rows = BotOrder.objects.filter(side="sell", status=BotOrder.Status.FILLED).values("bot_id").annotate(p=Sum("pnl"))
        if not rows:
            return "No closed trades yet."
        return "\n".join(f"#{r['bot_id']}: {r['p']:+,.2f}" for r in rows)
    if cmd == "/daily":
        n = int(args[0]) if args and args[0].isdigit() else 7
        since = dj_tz.now() - timedelta(days=n)
        rows = (BotOrder.objects.filter(side="sell", status=BotOrder.Status.FILLED, created_at__gte=since)
                .annotate(d=TruncDate("created_at")).values("d").annotate(p=Sum("pnl")).order_by("d"))
        return "\n".join(f"{r['d']}: {r['p']:+,.2f}" for r in rows) or f"No closed trades in the last {n} days."
    if cmd == "/balance":
        lines = []
        for b in _bots():
            st = b.state or {}
            pos = st.get("pos")
            lines.append(f"#{b.pk} cash {st.get('cash', 0):,.2f}" + (f", position {pos['qty'] * pos['avg']:,.2f} at cost" if pos else ""))
        return "\n".join(lines) or "No bots running."
    if cmd == "/stop":
        bots.halt("halted from Telegram", by="telegram")
        return "Entries halted. Open positions are still managed. /start to resume."
    if cmd == "/start":
        bots.resume(by="telegram")
        return "Entries allowed again."
    if cmd == "/kill":
        if args[:1] != ["confirm"]:
            return "This sells every open position at market and stops all bots. Send: /kill confirm"
        n = bots.kill(by="telegram")
        return f"Kill switch done: {n} position(s) sold, all bots stopped, entries halted."
    if cmd == "/forceexit":
        if not args or not args[0].isdigit():
            return "Usage: /forceexit <bot id>"
        b = Bot.objects.filter(pk=int(args[0])).first()
        if not b:
            return "No such bot."
        o = bots.force_exit(b, by="telegram")
        if o and o.status == BotOrder.Status.FILLED:
            return f"Sold {o.filled_qty:.6g} @ {o.avg_price:,.6g}, P&L {o.pnl:+,.2f}"
        return "No position to exit, or the order failed (see the bot page)."
    return "Unknown command. /help"
