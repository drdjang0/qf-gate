"""Fill listener: Gate.io pushes every change to your orders and every trade the moment it happens.

Gate.io has no HTTP webhook for fills; its push channel is the private WebSocket. For every user with an
active Gate.io key this keeps one connection subscribed to:
- spot.orders      -> a live cascade whose order changed is reconciled at once (fill booked, take-profit and
                      next level sent); a live bot whose stop on Gate.io fired is closed;
- spot.usertrades  -> the trade goes straight into the user's Portfolio ledger (keyed by Gate's trade id,
                      so the 5-minute sync never doubles it).
Reconnects by itself with backoff; the worker's 30-second checks stay as the safety net when it is down.

Run it as a long-lived service next to the worker and the live stream:
    python manage.py stream_account
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

log = logging.getLogger("accounts.stream")
PING_SECONDS = 10
REFRESH_SECONDS = 60
CHANNELS = ("spot.orders", "spot.usertrades")


def sign(secret: str, channel: str, event: str, ts: int) -> str:
    """Gate.io WebSocket v4 auth: HexEncode(HMAC_SHA512(secret, "channel=<c>&event=<e>&time=<t>"))."""
    return hmac.new(secret.encode(), f"channel={channel}&event={event}&time={ts}".encode(), hashlib.sha512).hexdigest()


def subscribe_msg(channel: str, pairs: list[str], key: str, secret: str, ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    return {"time": ts, "channel": channel, "event": "subscribe", "payload": pairs,
            "auth": {"method": "api_key", "KEY": key, "SIGN": sign(secret, channel, "subscribe", ts)}}


def _listeners() -> dict[int, dict]:
    """{key id: {owner, key, secret, pairs}} for every active user's active Gate.io key."""
    close_old_connections()
    from accounts.crypto import KeyStoreError
    from accounts.models import ExchangeKey
    from market.models import Coin

    pairs = sorted({c.pair for c in Coin.objects.filter(is_active=True)})
    out = {}
    for k in ExchangeKey.objects.filter(is_active=True, owner__is_active=True, exchange="gate"):
        try:
            key, secret = k.credentials()
        except KeyStoreError as exc:
            log.warning("Key %s can't be decrypted: %s", k.pk, exc)
            continue
        out[k.pk] = {"owner_id": k.owner_id, "key": key, "secret": secret, "pairs": pairs}
    return out


def handle_trade(owner_id: int, item: dict) -> int:
    close_old_connections()
    from django.contrib.auth import get_user_model

    from ledger import gate_sync, recorder

    owner = get_user_model().objects.filter(pk=owner_id).first()
    acc = recorder.account(recorder.GATE, owner)
    return gate_sync.store(acc, "spot_trades", [item])


def handle_order(owner_id: int, item: dict) -> int:
    close_old_connections()
    from cascades import live
    from trading import bots as bots_mod
    from trading.models import Bot

    n = live.on_order_event(item)
    pair = (item.get("currency_pair") or "").upper()
    for b in Bot.objects.filter(status=Bot.Status.RUNNING, mode=Bot.Mode.LIVE, config__owner_id=owner_id,
                                config__coin__isnull=False).select_related("config__coin"):
        if b.config.coin.pair == pair and (b.pos or {}).get("stop_order"):
            try:
                n += bool(bots_mod.check_exchange_stop(b))
            except Exception:  # noqa: BLE001
                log.exception("Bot %s stop check failed", b.pk)
    return n


async def listen(kid: int, cfg: dict, url: str) -> None:
    import websockets

    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2 ** 22, open_timeout=20) as ws:
                for ch in CHANNELS:
                    await ws.send(json.dumps(subscribe_msg(ch, cfg["pairs"], cfg["key"], cfg["secret"])))

                async def pinger():
                    while True:
                        await asyncio.sleep(PING_SECONDS)
                        await ws.send(json.dumps({"time": int(time.time()), "channel": "spot.ping"}))

                ping = asyncio.create_task(pinger())
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("error"):
                            log.warning("Key %s %s: %s", kid, msg.get("channel"), msg["error"])
                            if (msg["error"] or {}).get("code") == 4:  # authentication failed: don't hammer Gate
                                await asyncio.sleep(300)
                                break
                            continue
                        if msg.get("event") == "subscribe":
                            backoff = 1
                            log.info("Key %s subscribed to %s", kid, msg.get("channel"))
                            continue
                        if msg.get("event") != "update":
                            continue
                        result = msg.get("result") or []
                        items = result if isinstance(result, list) else [result]
                        for item in items:
                            if msg.get("channel") == "spot.usertrades":
                                await asyncio.to_thread(handle_trade, cfg["owner_id"], item)
                            elif msg.get("channel") == "spot.orders":
                                await asyncio.to_thread(handle_order, cfg["owner_id"], item)
                finally:
                    ping.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect
            log.warning("Key %s listener: %s (reconnecting in %ss)", kid, exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def main(url: str, once: bool = False) -> None:
    from market.services.jobs import beat

    tasks: dict[int, tuple[asyncio.Task, tuple]] = {}
    while True:
        wanted = await asyncio.to_thread(_listeners)
        for kid in list(tasks):
            task, sig = tasks[kid]
            cfg = wanted.get(kid)
            if cfg is None or sig != (cfg["key"], tuple(cfg["pairs"])):
                task.cancel()
                del tasks[kid]
        for kid, cfg in wanted.items():
            if kid not in tasks:
                tasks[kid] = (asyncio.create_task(listen(kid, cfg, url)), (cfg["key"], tuple(cfg["pairs"])))
        await asyncio.to_thread(beat, "account_stream", f"{len(tasks)} account(s)")
        if once:
            for task, _ in tasks.values():
                task.cancel()
            return
        await asyncio.sleep(REFRESH_SECONDS)


class Command(BaseCommand):
    help = "Listen to Gate.io's private WebSocket for every user's order fills and trades (live cascades, bots, ledger)."

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        url = settings.GATE["WS_URL"]
        self.stdout.write(f"Fill listener started ({url})")
        try:
            asyncio.run(main(url))
        except KeyboardInterrupt:
            self.stdout.write("Fill listener stopped.")
