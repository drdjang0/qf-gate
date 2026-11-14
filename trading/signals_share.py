"""Signal sharing between QF-gate instances (idea from freqtrade's producer / consumer mode).

Producer: every running paper session and bot records the signals its engine queues ("local").
They are served at GET /trading/api/v1/signals?since=<id> to callers holding SIGNAL_API_TOKEN, or pushed
to another instance's POST /trading/api/v1/signals/ingest.

Consumer: `manage.py follow_signals --url ... --name ...` polls a producer (or the producer pushes), the
signals are stored with that source name, and every running bot whose strategy is "Follow remote
signals" on the same pair executes them through its broker, with the usual risk limits and kill switch.
"""

from __future__ import annotations

import hmac
import logging

from django.conf import settings
from django.db import IntegrityError, transaction

from .models import Bot, LiveSignal

log = logging.getLogger("trading.signals")


def token_ok(header: str | None) -> bool:
    token = settings.TRADING.get("SIGNAL_API_TOKEN") or ""
    if not token or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), token)


def record(config_name: str, pair: str, interval: str, pending: dict | None, bar_time: int | None) -> None:
    """Called after a paper / bot step: store the signal the engine just queued (once)."""
    if not pending or bar_time is None or pending.get("action") not in ("buy", "sell", "short", "cover"):
        return
    try:
        with transaction.atomic():
            LiveSignal.objects.create(source="local", config_name=config_name[:100], pair=pair, interval=interval,
                                      action=pending["action"], stop=pending.get("stop"), target=pending.get("target"),
                                      reason=(pending.get("reason") or "")[:255], bar_time=bar_time)
    except IntegrityError:
        pass  # already recorded by an earlier step


def serialize(s: LiveSignal) -> dict:
    return {"id": s.pk, "config": s.config_name, "pair": s.pair, "interval": s.interval, "action": s.action,
            "stop": s.stop, "target": s.target, "reason": s.reason, "bar_time": s.bar_time}


def ingest(source: str, items: list[dict]) -> list[LiveSignal]:
    """Store signals received from another instance; returns the new ones."""
    new = []
    for it in items:
        if not isinstance(it, dict) or it.get("action") not in ("buy", "sell", "short", "cover") or not it.get("pair"):
            continue
        try:
            with transaction.atomic():
                new.append(LiveSignal.objects.create(
                    source=source[:64], config_name=str(it.get("config", ""))[:100], pair=str(it["pair"])[:40],
                    interval=str(it.get("interval", ""))[:4], action=it["action"], stop=it.get("stop"),
                    target=it.get("target"), reason=str(it.get("reason", ""))[:255], bar_time=int(it["bar_time"]),
                    remote_id=it.get("id")))
        except (IntegrityError, KeyError, TypeError, ValueError):
            continue
    return new


def dispatch(broker=None) -> int:
    """Hand new remote signals to running 'follow' bots on the same pair. Returns orders sent."""
    from . import bots

    sent = 0
    for s in LiveSignal.objects.exclude(source="local").filter(acted=False).order_by("id"):
        LiveSignal.objects.filter(pk=s.pk).update(acted=True)  # act once, even if no bot follows it
        for b in Bot.objects.filter(status=Bot.Status.RUNNING, strategy_key="follow_remote").select_related("config__coin"):
            if b.config.coin.pair != s.pair:
                continue
            why = f"remote {s.source}: {s.reason}"[:255]
            if s.action == "buy":
                o = bots.execute(b.pk, "buy", why, stop=s.stop, target=s.target, broker=broker)
            elif s.action in ("sell", "cover"):
                o = bots.execute(b.pk, "sell", why, broker=broker)
            else:
                log.info("Signal %s: shorts are not followed by spot bots", s.pk)
                continue
            sent += bool(o)
    return sent
