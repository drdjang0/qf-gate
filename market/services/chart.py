"""Chart data: candles at any display interval, aggregated from stored 1m data.

Aggregating from the stored 1m series (instead of fetching each interval from
Gate) keeps every timeframe consistent with the daily levels. Where a minute
has no 1m candle (fallback days), the stored 5m candle covering it is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings

from ..models import Candle, Coin, DailyLevel

CHART_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
DEFAULT_DAYS = {"1m": 1, "5m": 3, "15m": 10, "1h": 30, "4h": 60, "1d": 60}
MAX_DAYS = {"1m": 7, "5m": 30, "15m": 60, "1h": 120, "4h": 365, "1d": 365}


def _bucket(ts: int, step: int, offset: int) -> int:
    """Bucket start for ts. 4h/1d buckets follow the configured day timezone (offset seconds)."""
    return (ts + offset) // step * step - offset


def chart_candles(coin: Coin, interval: str, start: datetime, end: datetime, tz_name: str | None = None) -> list[dict]:
    step = CHART_INTERVALS[interval]
    tz = ZoneInfo(tz_name or settings.MARKET["DAY_TIMEZONE"])
    cols = ("open_time", "open", "high", "low", "close", "volume_base", "volume_quote", "buy_volume", "sell_volume")
    rows_1m = list(
        Candle.objects.filter(coin=coin, interval="1m", open_time__gte=start, open_time__lt=end)
        .order_by("open_time")
        .values_list(*cols)
    )
    have = {int(r[0].timestamp()) for r in rows_1m}
    rows_5m = [
        r
        for r in Candle.objects.filter(coin=coin, interval="5m", open_time__gte=start, open_time__lt=end)
        .order_by("open_time")
        .values_list(*cols)
        if not any(int(r[0].timestamp()) + k * 60 in have for k in range(5))
    ]
    rows = sorted(rows_1m + rows_5m, key=lambda r: r[0]) if rows_5m else rows_1m

    out: dict[int, dict] = {}
    for t, o, h, lo, c, v, q, bv, sv in rows:
        ts = int(t.timestamp())
        offset = int(t.astimezone(tz).utcoffset().total_seconds()) if step >= 14400 else 0
        b = _bucket(ts, step, offset)
        bar = out.get(b)
        if bar is None:
            out[b] = {"time": b, "open": float(o), "high": float(h), "low": float(lo), "close": float(c), "volume": float(v),
                      "quote_volume": float(q),
                      "buy": float(bv) if bv is not None else None, "sell": float(sv) if sv is not None else None}
        else:
            bar["high"] = max(bar["high"], float(h))
            bar["low"] = min(bar["low"], float(lo))
            bar["close"] = float(c)
            bar["volume"] += float(v)
            bar["quote_volume"] += float(q)
            if bv is not None:
                bar["buy"] = (bar["buy"] or 0.0) + float(bv)
                bar["sell"] = (bar["sell"] or 0.0) + float(sv)
    return [out[k] for k in sorted(out)]


def chart_levels(coin: Coin, start: datetime, end: datetime, tz_name: str | None = None) -> list[dict]:
    """Per-day high/low and previous-day high/low (PDH/PDL) with day start/end timestamps."""
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    tz = ZoneInfo(tz_name)
    first = start.astimezone(tz).date() - timedelta(days=1)
    rows = list(
        DailyLevel.objects.filter(
            coin=coin, timezone_name=tz_name, date__gte=first, date__lte=end.astimezone(tz).date()
        ).order_by("date")
    )
    out, prev = [], None
    for r in rows:
        d0 = datetime(r.date.year, r.date.month, r.date.day, tzinfo=tz)
        nxt = r.date + timedelta(days=1)
        d1 = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
        adjacent = prev is not None and prev.date == r.date - timedelta(days=1)
        if d1 > start:
            out.append({
                "date": r.date.isoformat(),
                "start": int(d0.timestamp()),
                "end": int(d1.timestamp()),
                "high": float(r.high),
                "low": float(r.low),
                "high_time": int(r.high_time.timestamp()),
                "low_time": int(r.low_time.timestamp()),
                "pdh": float(prev.high) if adjacent else None,
                "pdl": float(prev.low) if adjacent else None,
                "complete": r.is_complete,
            })
        prev = r
    return out


def window(interval: str, days: int | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    days = max(1, min(days or DEFAULT_DAYS[interval], MAX_DAYS[interval]))
    return now - timedelta(days=days), now + timedelta(minutes=1)
