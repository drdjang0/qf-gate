"""Day-by-day history of a coin: OHLC, high/low times, change, range, volume, PDH/PDL breaks.

Built from the stored daily levels (open/high/low/close + times) and the stored
1m candles (volume), so it matches the chart and the strategies exactly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate

from ..models import Candle, Coin, DailyLevel


def daily_volume(coin: Coin, tz: ZoneInfo, start: datetime, end: datetime) -> dict:
    """{day: (base volume, quote volume)} summed in the database (fast for a year of 1m candles).
    Per day the 1m candles are used, or the 5m candles where 1m coverage is short (fallback days)."""
    def sums(interval):
        return {
            r["d"]: (r["n"], float(r["vb"]), float(r["vq"]),
                     # order-flow delta only when every candle of the day has the split
                     float(r["buy"] - r["sell"]) if r["nf"] and r["nf"] == r["n"] else None)
            for r in Candle.objects.filter(coin=coin, interval=interval, open_time__gte=start, open_time__lt=end)
            .annotate(d=TruncDate("open_time", tzinfo=tz)).values("d")
            .annotate(n=Count("id"), vb=Sum("volume_base"), vq=Sum("volume_quote"),
                      buy=Sum("buy_volume"), sell=Sum("sell_volume"), nf=Count("buy_volume"))
        }

    one, five = sums("1m"), sums("5m")
    out = {}
    for d in set(one) | set(five):
        n1, b1, q1, f1 = one.get(d, (0, 0.0, 0.0, None))
        n5, b5, q5, f5 = five.get(d, (0, 0.0, 0.0, None))
        out[d] = (b1, q1, f1 if n1 >= 1440 else None) if n1 * 60 >= n5 * 300 else (b5, q5, None)
    return out


def daily_history(coin: Coin, tz_name: str | None = None) -> list[dict]:
    """Newest first. Change % is against the previous day's close (only when that day is stored)."""
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    tz = ZoneInfo(tz_name)
    levels = list(DailyLevel.objects.filter(coin=coin, timezone_name=tz_name).order_by("date"))
    if not levels:
        return []
    first, last = levels[0].date, levels[-1].date + timedelta(days=1)
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    end = datetime(last.year, last.month, last.day, tzinfo=tz)
    volume = daily_volume(coin, tz, start, end)

    rows, prev = [], None
    for lv in levels:
        adjacent = prev is not None and prev.date == lv.date - timedelta(days=1)
        high, low, close = float(lv.high), float(lv.low), float(lv.close)
        pdh = float(prev.high) if adjacent else None
        pdl = float(prev.low) if adjacent else None
        prev_close = float(prev.close) if adjacent else None
        vb, vq, delta = volume.get(lv.date, (None, None, None))
        rows.append({
            "level": lv,
            "change_pct": (close - prev_close) / prev_close * 100 if prev_close else None,
            "range_pct": (high - low) / low * 100 if low else None,
            "volume": vb,
            "quote_volume": vq,
            "delta": delta,
            "delta_pct": delta / vb * 100 if delta is not None and vb else None,
            "pdh": prev.high if adjacent else None,
            "pdl": prev.low if adjacent else None,
            "broke_pdh": pdh is not None and high > pdh,
            "broke_pdl": pdl is not None and low < pdl,
            "start": int(datetime(lv.date.year, lv.date.month, lv.date.day, tzinfo=tz).timestamp()),
        })
        prev = lv
    rows.reverse()
    return rows


def summarize(rows: list[dict]) -> dict | None:
    """Totals over the history rows: up/down days, PDH/PDL breaks, average daily range."""
    if not rows:
        return None
    ranges = [r["range_pct"] for r in rows if r["range_pct"] is not None]
    with_prev = [r for r in rows if r["pdh"] is not None]
    return {
        "days": len(rows),
        "up": sum(1 for r in rows if r["change_pct"] is not None and r["change_pct"] > 0),
        "down": sum(1 for r in rows if r["change_pct"] is not None and r["change_pct"] < 0),
        "avg_range_pct": sum(ranges) / len(ranges) if ranges else None,
        "with_prev": len(with_prev),
        "broke_pdh": sum(1 for r in with_prev if r["broke_pdh"]),
        "broke_pdl": sum(1 for r in with_prev if r["broke_pdl"]),
        "high": max(rows, key=lambda r: r["level"].high)["level"],
        "low": min(rows, key=lambda r: r["level"].low)["level"],
    }
