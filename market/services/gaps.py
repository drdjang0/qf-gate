"""Data-quality report: how complete the stored candles are, day by day (UTC days).

complete  - all 1440 one-minute candles (or every minute so far, for today)
fallback  - 1m is short but the day is fully covered by 5m candles (timestamps to 5 minutes)
partial   - some candles, but neither 1m nor 5m covers the whole day
missing   - no candles at all inside the stored range
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from django.db.models import Count
from django.db.models.functions import TruncDate

from ..models import Candle, Coin


def _counts(coin: Coin, interval: str, start: datetime) -> dict[date, int]:
    return {
        r["d"]: r["n"]
        for r in Candle.objects.filter(coin=coin, interval=interval, open_time__gte=start)
        .annotate(d=TruncDate("open_time", tzinfo=timezone.utc)).values("d").annotate(n=Count("id"))
    }


def coverage(coin: Coin, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    first = Candle.objects.filter(coin=coin, interval__in=("1m", "5m")).order_by("open_time").values_list(
        "open_time", flat=True).first()
    if first is None:
        return {"days": [], "summary": {}, "first_gap": None, "problems": []}
    start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    one, five = _counts(coin, "1m", start), _counts(coin, "5m", start)
    today = now.date()
    rows = []
    d = start.date()
    while d <= today:
        if d == today:
            # minutes that have fully closed so far today
            expected_1m = max(1, int((now - datetime(d.year, d.month, d.day, tzinfo=timezone.utc)).total_seconds() // 60))
        else:
            expected_1m = 1440
        expected_5m = max(1, expected_1m // 5)
        n1, n5 = one.get(d, 0), five.get(d, 0)
        if n1 >= expected_1m - (1 if d == today else 0):
            status = "complete"
        elif n5 >= expected_5m and n1 + n5 > 0:
            status = "fallback"
        elif n1 or n5:
            status = "partial"
        else:
            status = "missing"
        rows.append({"date": d, "m1": n1, "m5": n5, "expected": expected_1m,
                     "pct": min(100.0, n1 / expected_1m * 100), "status": status})
        d += timedelta(days=1)
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in ("complete", "fallback", "partial", "missing")}
    gaps = [r["date"] for r in rows if r["status"] in ("partial", "missing")]
    return {"days": rows, "summary": summary, "first_gap": gaps[0] if gaps else None,
            "problems": [r for r in rows if r["status"] != "complete"]}


def repair_days(report: dict, today: date | None = None) -> int | None:
    """How many days back a sync must reach to cover the oldest gap (None if there is none)."""
    if not report.get("first_gap"):
        return None
    today = today or datetime.now(timezone.utc).date()
    return (today - report["first_gap"]).days + 1
