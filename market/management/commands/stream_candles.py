"""Live 1m candles over Gate.io WebSocket (low-latency feed for analytics and bots).

- Subscribes `spot.candlesticks` 1m for every active, verified coin. Gate pushes
  the forming candle every ~2-4 s; it is upserted each time (is_closed=False)
  and marked closed when the next minute starts.
- On every (re)connect it back-fills missed minutes over REST first, so a
  dropped connection never leaves a hole in the 1m series.
- Today's daily high/low is recomputed whenever a minute closes.
- Keeps the connection alive with spot.ping and reconnects with backoff.
- Re-reads the coin list every 60 s; adding/removing a coin triggers a resubscribe.

Run it as a long-lived service (systemd / NSSM / pm2 / supervisor):
    python manage.py stream_candles
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db.models import Max

from market.exchanges.gate import get_client, parse_ws_candle
from market.models import Candle, Coin
from market.signals import candle_closed, price_tick
from market.services.jobs import beat
from market.services.levels import compute_levels
from market.services.sync import upsert_candles

log = logging.getLogger("market.stream")
PING_SECONDS = 10
COIN_REFRESH_SECONDS = 60


def _active_coins() -> dict[str, Coin]:
    close_old_connections()
    return {c.pair: c for c in Coin.objects.filter(is_active=True, verified_at__isnull=False)}


def _levels_today(coin: Coin) -> None:
    today = datetime.now(timezone.utc).date()
    # yesterday too, so the previous day is finalised right after the boundary
    compute_levels(coin, today - timedelta(days=1), today + timedelta(days=1))


def _backfill(coin: Coin) -> int:
    """Fetch 1m candles missed while disconnected (REST), from the last stored closed candle."""
    close_old_connections()
    client = get_client()
    now = datetime.now(timezone.utc)
    last = Candle.objects.filter(coin=coin, interval="1m", is_closed=True).aggregate(m=Max("open_time"))["m"]
    start = max(last or now - timedelta(days=1), client.earliest_available("1m", now))
    n = upsert_candles(coin, "1m", client.get_candles(coin.pair, "1m", start, now), Candle.Source.REST)
    _levels_today(coin)
    return n


def _store(coin: Coin, candle) -> None:
    close_old_connections()
    upsert_candles(coin, "1m", [candle], Candle.Source.WS)
    for receiver, result in price_tick.send_robust(sender=Coin, coin=coin, price=float(candle.close)):
        if isinstance(result, Exception):
            log.error("price_tick receiver %s failed: %s", receiver, result)


def _store_flow(coin: Coin, minute: datetime, buy: float, sell: float) -> None:
    """Final taker buy/sell volume of a closed minute, from the live trade feed."""
    close_old_connections()
    Candle.objects.filter(coin=coin, interval="1m", open_time=minute).update(
        buy_volume=Decimal(str(buy)), sell_volume=Decimal(str(sell)))


def _beat(info: str) -> None:
    close_old_connections()
    beat("stream", info)


def _on_minute_closed(coin: Coin, new_open: datetime) -> None:
    close_old_connections()
    Candle.objects.filter(coin=coin, interval="1m", is_closed=False, open_time__lt=new_open).update(is_closed=True)
    _levels_today(coin)
    for receiver, result in candle_closed.send_robust(sender=Coin, coin=coin, open_time=new_open):
        if isinstance(result, Exception):
            log.error("candle_closed receiver %s failed: %s", receiver, result)


class Command(BaseCommand):
    help = "Stream live 1m candles from Gate.io WebSocket into the database."

    def handle(self, *args, **opts):
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")

    async def _run(self):
        backoff = 1
        while True:
            coins = await sync_to_async(_active_coins)()
            if not coins:
                self.stdout.write("No active verified coins; checking again in 60 s")
                await asyncio.sleep(COIN_REFRESH_SECONDS)
                continue
            try:
                await self._session(coins)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                log.warning("Stream error: %s; reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self, coins: dict[str, Coin]):
        import websockets

        url = settings.GATE["WS_URL"]
        async with websockets.connect(url, ping_interval=None, close_timeout=5, max_size=2**22) as ws:
            for pair in coins:
                await ws.send(json.dumps({
                    "time": int(time.time()), "channel": "spot.candlesticks",
                    "event": "subscribe", "payload": ["1m", pair],
                }))
            # order flow: every public trade, summed per minute into taker buy / sell volume
            await ws.send(json.dumps({"time": int(time.time()), "channel": "spot.trades",
                                      "event": "subscribe", "payload": list(coins)}))
            flow: dict[tuple[str, int], list[float]] = {}
            first_full_minute = (int(time.time()) // 60 + 1) * 60  # the minute we joined mid-way is incomplete
            # back-fill after subscribing, so nothing between REST and the stream is lost
            for coin in coins.values():
                n = await sync_to_async(_backfill)(coin)
                self.stdout.write(f"{coin.pair}: back-filled {n} candle(s) over REST")
            self.stdout.write(self.style.SUCCESS(f"Streaming 1m candles for {', '.join(coins)}"))

            current_open: dict[str, datetime] = {}
            last_ping = last_refresh = time.monotonic()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    raw = None
                now = time.monotonic()
                if now - last_ping >= PING_SECONDS:
                    await ws.send(json.dumps({"time": int(time.time()), "channel": "spot.ping"}))
                    await sync_to_async(_beat)(f"{len(coins)} coin(s)")
                    last_ping = now
                if now - last_refresh >= COIN_REFRESH_SECONDS:
                    last_refresh = now
                    latest = await sync_to_async(_active_coins)()
                    if set(latest) != set(coins):
                        self.stdout.write("Coin list changed; resubscribing")
                        return
                if raw is None:
                    continue
                msg = json.loads(raw)
                if msg.get("channel") == "spot.trades" and msg.get("event") == "update":
                    t = msg.get("result") or {}
                    minute = int(float(t.get("create_time_ms") or 0) / 1000) // 60 * 60
                    acc = flow.setdefault((t.get("currency_pair"), minute), [0.0, 0.0])
                    acc[0 if t.get("side") == "buy" else 1] += float(t.get("amount") or 0)
                    continue
                if msg.get("channel") != "spot.candlesticks" or msg.get("event") != "update":
                    if msg.get("error"):
                        log.error("Gate WS error: %s", msg["error"])
                    continue
                interval, pair, candle = parse_ws_candle(msg["result"])
                coin = coins.get(pair)
                if coin is None or interval != "1m":
                    continue
                await sync_to_async(_store)(coin, candle)
                prev = current_open.get(pair)
                if prev is not None and candle.open_time > prev:
                    await sync_to_async(_on_minute_closed)(coin, candle.open_time)
                    key = (pair, int(prev.timestamp()))
                    if key[1] >= first_full_minute and key in flow:
                        await sync_to_async(_store_flow)(coin, prev, *flow[key])
                    for k in [k for k in flow if k[0] == pair and k[1] < int(candle.open_time.timestamp())]:
                        del flow[k]
                current_open[pair] = candle.open_time
