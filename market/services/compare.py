"""Gate.io vs another exchange (read-only): daily closes, volume and order flow side by side.

Coins are matched by ticker on the other exchange (it has no contract addresses), so the match is
only trusted when prices agree: a median close difference above MAX_MEDIAN_DIFF_PCT means it is
probably a different token with the same ticker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from django.core.cache import cache

from ..exchanges.binance import BinanceClient
from ..models import Candle, Coin

MAX_MEDIAN_DIFF_PCT = 2.0
CACHE_SECONDS = 600


def symbol_for(coin: Coin) -> str:
    return f"{(coin.gate_currency or coin.symbol).upper()}{coin.quote.upper()}"


def compare_daily(coin: Coin, days: int = 30, client=None) -> dict:
    key = f"compare:binance:{coin.pk}:{days}"
    cached = cache.get(key)
    if cached is not None and client is None:
        return cached
    end = datetime.now(timezone.utc)
    start = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    symbol = symbol_for(coin)
    other = (client or BinanceClient()).get_candles(symbol, "1d", start, end)
    if other is None:
        return {"symbol": symbol, "listed": False, "rows": []}
    theirs = {c.open_time.date(): c for c in other}
    ours = {c.open_time.date(): c for c in Candle.objects.filter(coin=coin, interval="1d", open_time__gte=start)}
    rows = []
    for d in sorted(set(ours) & set(theirs), reverse=True):
        g, b = ours[d], theirs[d]
        gc, bc = float(g.close), float(b.close)
        rows.append({
            "date": d, "gate_close": gc, "other_close": bc, "diff_pct": (gc / bc - 1) * 100 if bc else None,
            "gate_volume": float(g.volume_quote), "other_volume": float(b.volume_quote),
            "other_delta_pct": float((b.buy_volume - b.sell_volume) / b.volume_base * 100) if b.volume_base else None,
            "complete": g.is_closed and b.is_closed,
        })
    diffs = [abs(r["diff_pct"]) for r in rows if r["diff_pct"] is not None]
    med = median(diffs) if diffs else None
    result = {
        "symbol": symbol, "listed": True, "rows": rows, "median_diff_pct": med,
        "same_asset": med is not None and med <= MAX_MEDIAN_DIFF_PCT,
        "volume_share_pct": (sum(r["gate_volume"] for r in rows) /
                             max(sum(r["gate_volume"] + r["other_volume"] for r in rows), 1e-9) * 100) if rows else None,
    }
    if client is None:
        cache.set(key, result, CACHE_SECONDS)
    return result
