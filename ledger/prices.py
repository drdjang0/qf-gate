"""Prices for valuing holdings: live Gate.io tickers, and a historical price for a past moment."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.core.cache import cache

from market.exchanges.base import ExchangeError
from market.exchanges.gate import get_client
from market.models import INTERVAL_SECONDS

LIVE_CACHE_SECONDS = 30


def live_prices(client=None) -> dict[str, Decimal]:
    """{"ETH_USDT": Decimal(last)} for every Gate.io spot pair (one request, cached 30 s)."""
    hit = cache.get("ledger:tickers")
    if hit is not None:
        return hit
    prices = {}
    try:
        for t in (client or get_client()).get_all_tickers():
            if t.get("last") not in (None, ""):
                prices[t["currency_pair"]] = Decimal(str(t["last"]))
    except ExchangeError:
        return {}
    cache.set("ledger:tickers", prices, LIVE_CACHE_SECONDS)
    return prices


def price_at(pair: str, at: datetime, client=None) -> Decimal | None:
    """Close of the finest Gate.io candle still available that contains `at` (an estimate, not a fill)."""
    client = client or get_client()
    for interval in ("1m", "1h", "1d"):
        if at < client.earliest_available(interval):
            continue
        step = INTERVAL_SECONDS[interval]
        start = datetime.fromtimestamp(int(at.timestamp()) // step * step, tz=at.tzinfo)
        try:
            rows = client.get_candles(pair, interval, start, start + timedelta(seconds=step - 1))
        except ExchangeError:
            return None
        if rows:
            return Decimal(str(rows[0].close))
    return None
