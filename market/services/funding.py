"""Futures funding rates from Gate.io (USDT perpetuals), stored per coin for futures backtests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..exchanges.gate import get_client
from ..models import Coin, FundingRate

MAX_DAYS = 179  # Gate serves the last 180 days


def sync_funding(coin: Coin, client=None) -> int:
    """Fetch the last ~180 days of funding for the coin's perpetual contract. Returns rows stored."""
    client = client or get_client()
    now = datetime.now(timezone.utc)
    start = int((now - timedelta(days=MAX_DAYS)).timestamp())
    rows = client._get("/futures/usdt/funding_rate",
                       {"contract": coin.pair, "limit": 1000, "from": start, "to": int(now.timestamp())}, retries=2)
    if not rows:
        return 0
    objs = [FundingRate(coin=coin, time=datetime.fromtimestamp(int(r["t"]), tz=timezone.utc), rate=float(r["r"]))
            for r in rows]
    FundingRate.objects.bulk_create(objs, update_conflicts=True, unique_fields=["coin", "time"], update_fields=["rate"])
    return len(objs)


def funding_map(coin: Coin, start: datetime, end: datetime) -> dict[int, float]:
    """{unix time: rate} for the engine, snapped to the 8-hour boundary (Gate's stamps can be a second off)."""
    every = 8 * 3600
    return {round(t.timestamp() / every) * every: r for t, r in
            FundingRate.objects.filter(coin=coin, time__gte=start - timedelta(minutes=5),
                                       time__lte=end + timedelta(minutes=5)).values_list("time", "rate")}


def sync_all() -> int:
    return sum(sync_funding(c) for c in Coin.objects.filter(is_active=True, verified_at__isnull=False))
