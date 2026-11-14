"""Price zones from past daily highs/lows: how often price came back, and what it did there.

Each stored day gives a High zone and a Low zone: price x (1 +/- width). Unlike a plain
"all candles against all candles" count, everything here is **forward-only** (no hindsight):

- a zone can only be touched by days *after* the day that formed it;
- a day *touches* a zone when its range [low, high] overlaps the zone, so a candle that
  trades straight through the zone counts too;
- the touch time is the first minute price entered the zone (from stored 1m candles; 5m
  on fallback days);
- each touch gets a result, judged on that day's close against where price came from
  (the previous close):
    from below: "broke" if it closed above the zone, else "held";
    from above: "broke" if it closed below the zone, else "held";
    previous close already inside the zone: "inside"; today's unfinished day: "live".

Hotspots are the price bands (same width) where the most later days came back, picked so
that no two overlap. Each band counts distinct days, so overlapping zones can't inflate it.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max

from ..models import Candle, Coin, DailyLevel
from .chart import chart_candles

DEFAULT_WIDTH_PCT = 1.0
DEFAULT_DAYS = 90
DAY_CHOICES = (30, 60, 90, 180, 365)
MIN_WIDTH_PCT, MAX_WIDTH_PCT = 0.1, 5.0
TOP_HOTSPOTS = 8


@dataclass
class Day:
    date: date
    open: float
    high: float
    low: float
    close: float
    complete: bool
    prev_close: float | None = None  # previous calendar day's close, if stored


@dataclass
class Minutes:
    """One day's intraday path, prepared for O(log n) 'first minute inside a zone' lookups."""

    times: list[datetime]
    open: float
    run_max: list[float]  # highest high so far
    neg_run_min: list[float]  # minus the lowest low so far (non-decreasing, for bisect)

    @classmethod
    def build(cls, bars: list[tuple[datetime, float, float, float]]) -> Minutes:
        times, run_max, neg_run_min = [], [], []
        hi, lo = -math.inf, math.inf
        for t, _o, h, low in bars:
            hi, lo = max(hi, h), min(lo, low)
            times.append(t)
            run_max.append(hi)
            neg_run_min.append(-lo)
        return cls(times, bars[0][1], run_max, neg_run_min)

    def first_entry(self, lower: float, upper: float) -> datetime | None:
        if lower <= self.open <= upper:
            return self.times[0]
        if self.open < lower:  # price has to rise into the zone
            i = bisect_left(self.run_max, lower)
        else:  # price has to fall into the zone
            i = bisect_left(self.neg_run_min, -upper)
        return self.times[i] if i < len(self.times) else None


@dataclass
class Touch:
    date: date
    time: datetime | None
    result: str  # held | broke | inside | live


@dataclass
class Zone:
    kind: str  # "high" | "low"
    date: date
    price: float
    level_time: datetime | None
    lower: float
    upper: float
    touches: list[Touch] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(1 for t in self.touches if t.result == result)

    @property
    def held(self) -> int:
        return self.count("held")

    @property
    def broke(self) -> int:
        return self.count("broke")

    @property
    def last_touch(self) -> Touch | None:
        return self.touches[-1] if self.touches else None


def touch_result(prev_close: float | None, day: Day, lower: float, upper: float) -> str:
    if not day.complete:
        return "live"
    ref = prev_close if prev_close is not None else day.open
    if ref < lower:
        return "broke" if day.close > upper else "held"
    if ref > upper:
        return "broke" if day.close < lower else "held"
    return "inside"


def overlaps(day: Day, lower: float, upper: float) -> bool:
    return day.low <= upper and day.high >= lower


def build_zones(days: list[Day], levels: list[tuple[str, date, float, datetime | None]], width: float,
                minutes: dict[date, Minutes] | None = None) -> list[Zone]:
    """levels: (kind, date, price, time). days: oldest first. width: fraction (0.01 = 1%)."""
    minutes = minutes or {}
    zones = []
    for kind, d, price, t in levels:
        z = Zone(kind, d, price, t, price * (1 - width), price * (1 + width))
        for day in days:
            if day.date <= d or not overlaps(day, z.lower, z.upper):
                continue
            m = minutes.get(day.date)
            z.touches.append(Touch(
                day.date,
                m.first_entry(z.lower, z.upper) if m else None,
                touch_result(day.prev_close, day, z.lower, z.upper),
            ))
        zones.append(z)
    return zones


def hotspots(days: list[Day], zones: list[Zone], width: float, top: int = TOP_HOTSPOTS) -> list[dict]:
    """Bands where price returned most often after a level formed there. Non-overlapping."""
    cands = []
    for z in zones:
        lower, upper = z.lower, z.upper
        inside = [x for x in zones if lower <= x.price <= upper]
        formed = min(x.date for x in inside)
        hits = [d for d in days if d.date > formed and overlaps(d, lower, upper)]
        results = [touch_result(d.prev_close, d, lower, upper) for d in hits]
        cands.append({
            "center": z.price, "lower": lower, "upper": upper,
            "touches": len(hits),
            "held": results.count("held"), "broke": results.count("broke"),
            "highs": sum(1 for x in inside if x.kind == "high"),
            "lows": sum(1 for x in inside if x.kind == "low"),
            "formed": formed,
            "last_touch": hits[-1].date if hits else None,
        })
    cands.sort(key=lambda c: (c["touches"], c["highs"] + c["lows"], c["formed"]), reverse=True)
    chosen: list[dict] = []
    for c in cands:
        if len(chosen) >= top or c["touches"] == 0:
            break
        if any(c["lower"] <= o["upper"] and o["lower"] <= c["upper"] for o in chosen):
            continue
        chosen.append(c)
    return chosen


def heatmap(days: list[Day], zones: list[Zone], width: float) -> dict | None:
    """Rows = price bands one zone wide (geometric, so they suit any price), columns = days.
    Each cell counts the zones (by their level price) that price came back to on that day."""
    if not days or not zones:
        return None
    ratio = (1 + width) / (1 - width)
    base = min(z.price for z in zones)
    n_rows = int(math.log(max(z.price for z in zones) / base) / math.log(ratio)) + 1
    col = {d.date: i for i, d in enumerate(days)}
    grid = [[0] * len(days) for _ in range(n_rows)]
    for z in zones:
        r = min(n_rows - 1, int(math.log(z.price / base) / math.log(ratio)))
        for t in z.touches:
            grid[r][col[t.date]] += 1
    peak = max((v for row in grid for v in row), default=0) or 1
    rows = []
    for r in reversed(range(n_rows)):  # highest prices on top
        lo, hi = base * ratio**r, base * ratio ** (r + 1)
        rows.append({"lower": lo, "upper": hi,
                     "cells": [{"n": v, "a": round(v / peak, 3)} for v in grid[r]]})
    return {"days": [d.date for d in days], "rows": rows, "peak": peak}


# -- loading from the database --------------------------------------------------------------


def load_days(coin: Coin, tz_name: str, days: int | None = None) -> tuple[list[Day], list[DailyLevel]]:
    qs = DailyLevel.objects.filter(coin=coin, timezone_name=tz_name)
    if days:
        last = qs.order_by("-date").values_list("date", flat=True).first()
        if last:
            qs = qs.filter(date__gt=last - timedelta(days=days))
    levels = list(qs.order_by("date"))
    days, prev = [], None
    for lv in levels:
        prev_close = float(prev.close) if prev and prev.date == lv.date - timedelta(days=1) else None
        days.append(Day(lv.date, float(lv.open), float(lv.high), float(lv.low), float(lv.close),
                        lv.is_complete, prev_close))
        prev = lv
    return days, levels


def load_minutes(coin: Coin, tz_name: str, first: date, last: date) -> dict[date, Minutes]:
    tz = ZoneInfo(tz_name)
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    nxt = last + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    by_day: dict[date, list] = {}
    for b in chart_candles(coin, "1m", start, end, tz_name):
        t = datetime.fromtimestamp(b["time"], tz)
        by_day.setdefault(t.date(), []).append((t, b["open"], b["high"], b["low"]))
    return {d: Minutes.build(bars) for d, bars in by_day.items()}


def analyse(coin: Coin, width_pct: float = DEFAULT_WIDTH_PCT, tz_name: str | None = None,
            days: int = DEFAULT_DAYS) -> dict:
    """Cached until the daily levels change (the live stream recomputes today's level each minute)."""
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    stamp = DailyLevel.objects.filter(coin=coin, timezone_name=tz_name).aggregate(m=Max("computed_at"))["m"]
    key = f"zones:{coin.pk}:{tz_name}:{width_pct}:{days}:{stamp.timestamp() if stamp else 0}"
    result = cache.get(key)
    if result is None:
        result = _analyse(coin, width_pct, tz_name, days)
        cache.set(key, result, 600)
    return result


def _analyse(coin: Coin, width_pct: float, tz_name: str, days_back: int) -> dict:
    width = width_pct / 100
    days, rows = load_days(coin, tz_name, days_back)
    if not days:
        return {"days": [], "zones": [], "hotspots": [], "heatmap": None, "price": None,
                "width_pct": width_pct, "tz": tz_name}
    levels = [(k, lv.date, float(getattr(lv, k)), getattr(lv, f"{k}_time")) for lv in rows for k in ("high", "low")]
    minutes = load_minutes(coin, tz_name, days[0].date, days[-1].date)
    zones = build_zones(days, levels, width, minutes)
    last = (Candle.objects.filter(coin=coin, interval="1m").order_by("-open_time")
            .values_list("close", flat=True).first())
    return {
        "days": days,
        "zones": zones,
        "hotspots": hotspots(days, zones, width),
        "heatmap": heatmap(days, zones, width),
        "price": float(last) if last is not None else days[-1].close,
        "width_pct": width_pct,
        "tz": tz_name,
    }
