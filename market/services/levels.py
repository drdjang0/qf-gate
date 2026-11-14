"""Daily high / low (and the time each was hit) from stored candles.

For each day (in settings.MARKET["DAY_TIMEZONE"], UTC by default):
1. Pick the finest interval (1m > 5m > 15m > 1h) that fully covers the day.
   High/low prices are exact with any complete, day-aligned interval.
2. Find the first candle that hit the high (and the low).
3. Refine that timestamp with finer candles inside that candle's window when
   they are fully present (e.g. a 5m fallback day whose high falls inside a
   stretch that already has 1m candles gets a 1m-accurate time).
4. Cross-check with Gate.io's own 1d candle when the day boundary is UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from ..models import INTERVAL_SECONDS, Candle, Coin, DailyLevel

LEVEL_INTERVALS = ("1m", "5m", "15m", "1h")


@dataclass(frozen=True, slots=True)
class _C:
    t: datetime
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal


def _day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)  # wall-clock day (DST-safe)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _load(coin: Coin, interval: str, start: datetime, end: datetime) -> list[_C]:
    rows = (
        Candle.objects.filter(coin=coin, interval=interval, open_time__gte=start, open_time__lt=end)
        .order_by("open_time")
        .values_list("open_time", "open", "high", "low", "close")
    )
    return [_C(*r) for r in rows]


def _slice(candles: list[_C], start: datetime, end: datetime) -> list[_C]:
    return [c for c in candles if start <= c.t < end]


def _refine(series: dict[str, list[_C]], coarse: str, hit: _C, attr: str) -> tuple[datetime, str]:
    """Narrow the time of a high/low hit using finer, complete candles inside `hit`'s window."""
    step = INTERVAL_SECONDS[coarse]
    win_end = hit.t + timedelta(seconds=step)
    target = getattr(hit, attr)
    for fine in LEVEL_INTERVALS:
        fstep = INTERVAL_SECONDS[fine]
        if fstep >= step:
            break
        inside = _slice(series[fine], hit.t, win_end)
        if len(inside) == step // fstep:
            for c in inside:
                if getattr(c, attr) == target:
                    return c.t, fine
    return hit.t, coarse


def level_for_day(series: dict[str, list[_C]], d_start: datetime, d_end: datetime, now: datetime) -> dict | None:
    """Compute one day's level from per-interval candle lists (already loaded)."""
    upto = min(d_end, now)
    best: tuple[str, list[_C], bool] | None = None
    for interval in LEVEL_INTERVALS:
        step = INTERVAL_SECONDS[interval]
        if int(d_start.timestamp()) % step:  # interval not aligned with this day boundary
            continue
        candles = _slice(series[interval], d_start, d_end)
        if not candles:
            continue
        # candles that should exist by now (the still-forming candle counts too)
        expected = -(-int((upto - d_start).total_seconds()) // step)
        if len(candles) >= expected:
            best = (interval, candles, True)
            break
        if best is None or len(candles) * step > len(best[1]) * INTERVAL_SECONDS[best[0]]:
            best = (interval, candles, False)
    if best is None:
        return None
    interval, candles, covered = best
    hi = max(candles, key=lambda c: c.h)  # max()/min() return the FIRST extreme candle
    lo = min(candles, key=lambda c: c.l)
    hi_t, hi_prec = _refine(series, interval, hi, "h")
    lo_t, lo_prec = _refine(series, interval, lo, "l")
    return {
        "open": candles[0].o,
        "close": candles[-1].c,
        "high": hi.h,
        "high_time": hi_t,
        "low": lo.l,
        "low_time": lo_t,
        # worst of the two, so the column never overstates accuracy
        "time_precision": max((hi_prec, lo_prec), key=lambda i: INTERVAL_SECONDS[i]),
        "candle_count": len(candles),
        "is_complete": covered and now >= d_end,
    }


def compute_levels(
    coin: Coin, start: date, end: date, tz_name: str | None = None, now: datetime | None = None
) -> int:
    """(Re)compute DailyLevel rows for start..end inclusive. Returns rows written."""
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    range_start, _ = _day_bounds(start, tz)
    _, range_end = _day_bounds(end, tz)
    series = {i: _load(coin, i, range_start, range_end) for i in LEVEL_INTERVALS}
    daily: dict[date, _C] = {}
    if tz_name == "UTC":
        daily = {c.t.date(): c for c in _load(coin, "1d", range_start, range_end)}

    written = 0
    d = start
    while d <= end:
        d_start, d_end = _day_bounds(d, tz)
        if d_start > now:
            break
        lvl = level_for_day(series, d_start, d_end, now)
        if lvl:
            note = ""
            ref = daily.get(d)
            if ref and lvl["is_complete"] and (ref.h != lvl["high"] or ref.l != lvl["low"]):
                note = f"Gate 1d candle says H={ref.h.normalize()} L={ref.l.normalize()}"
            DailyLevel.objects.update_or_create(
                coin=coin, timezone_name=tz_name, date=d, defaults={**lvl, "cross_check": note}
            )
            written += 1
        d += timedelta(days=1)
    return written


def levels_with_previous(coin: Coin, tz_name: str | None = None, limit: int | None = None) -> list[dict]:
    """Daily levels newest first, each with the previous day's high/low (PDH/PDL)."""
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    rows = list(DailyLevel.objects.filter(coin=coin, timezone_name=tz_name).order_by("date"))
    out = []
    prev = None
    for r in rows:
        adjacent = prev is not None and prev.date == r.date - timedelta(days=1)
        out.append({"level": r, "pdh": prev.high if adjacent else None, "pdl": prev.low if adjacent else None})
        prev = r
    out.reverse()
    return out[:limit] if limit else out
