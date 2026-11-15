"""Scanner: analyse any coin on any supported exchange on demand, without adding or storing it.

Fetch the period's candles live, then run the same forward-only zone analysis as the Zones page on
those bars (each bar's high and low is a zone; a later bar touches it when its range overlaps; held /
broke from the close vs where price came from), plus hotspots, heatmap, period stats and a "where it
trades" price check across exchanges. If the coin looks worth it, it is added to QF-gate (Gate.io)
for stored history, the live feed and trading.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from ..exchanges import public
from ..exchanges.base import ExchangeError
from . import zones as Z

# period (days) -> (label, candle interval): about 90-180 bars each
PERIODS = {
    1: ("24 hours", "15m"), 2: ("2 days", "30m"), 3: ("3 days", "1h"), 7: ("7 days", "1h"),
    15: ("15 days", "4h"), 30: ("30 days", "4h"), 60: ("60 days", "1d"), 90: ("90 days", "1d"),
}
DEFAULT_DAYS = 30
CACHE_SECONDS = 120


def where_it_trades(base: str, quote: str) -> list[dict]:
    """Last price / 24h change / volume on every supported exchange (in parallel)."""
    key = f"scan:where:{base}:{quote}"
    rows = cache.get(key)
    if rows is not None:
        return rows

    def one(ex):
        try:
            t = public.ticker(ex.key, base, quote)
            return {"key": ex.key, "name": ex.name, "listed": t is not None, **(t or {}), "error": ""}
        except ExchangeError as exc:
            return {"key": ex.key, "name": ex.name, "listed": None, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=len(public.EXCHANGES)) as pool:
        rows = list(pool.map(one, public.EXCHANGES.values()))
    prices = sorted(float(r["last"]) for r in rows if r.get("listed") and r.get("last"))
    mid = prices[len(prices) // 2] if prices else None
    for r in rows:
        r["vs_median_pct"] = (float(r["last"]) / mid - 1) * 100 if mid and r.get("listed") and r.get("last") else None
        r["quote_volume"] = float(r["quote_volume"]) if r.get("quote_volume") else None
    total = sum(r["quote_volume"] or 0 for r in rows if r.get("listed"))
    for r in rows:
        r["volume_share_pct"] = (r["quote_volume"] or 0) / total * 100 if total and r.get("listed") else None
    cache.set(key, rows, CACHE_SECONDS)
    return rows


def scan(exchange: str, base: str, quote: str, days: int = DEFAULT_DAYS, width_pct: float = 1.0) -> dict:
    """Fetch and analyse. Raises ExchangeError when the exchange can't be reached."""
    days = days if days in PERIODS else DEFAULT_DAYS
    label, interval = PERIODS[days]
    base, quote = base.strip().upper(), quote.strip().upper()
    key = f"scan:{exchange}:{base}:{quote}:{days}:{width_pct}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    raw = public.candles(exchange, base, quote, interval, start, end)
    result = {"exchange": exchange, "exchange_name": public.EXCHANGES[exchange].name, "base": base, "quote": quote,
              "days": days, "label": label, "interval": interval, "width_pct": width_pct, "listed": raw is not None}
    if not raw:
        result.update(bars=[], zones=[], hotspots=[], heatmap=None, stats=None)
        return result
    bars, prev_close = [], None
    for c in raw:
        bars.append(Z.Day(c.open_time, float(c.open), float(c.high), float(c.low), float(c.close), c.is_closed, prev_close))
        prev_close = float(c.close)
    closed = [b for b in bars if b.complete]
    levels = [(k, b.date, getattr(b, k), b.date) for b in closed for k in ("high", "low")]
    width = width_pct / 100
    zones = Z.build_zones(bars, levels, width)
    last = bars[-1].close
    hot = Z.hotspots(bars, zones, width)
    for h in hot:
        h["dist"] = ((h["lower"] + h["upper"]) / 2 / last - 1) * 100
        h["here"] = h["lower"] <= last <= h["upper"]
    touches = [t for z in zones for t in z.touches]
    decided = [t for t in touches if t.result in ("held", "broke")]
    hi, lo = max(b.high for b in bars), min(b.low for b in bars)
    result.update(
        bars=[{"time": int(b.date.timestamp()), "open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in bars],
        zones=sorted(zones, key=lambda z: -len(z.touches))[:30],
        hotspots=hot,
        heatmap=Z.heatmap(bars, zones, width),
        stats={
            "first": bars[0].open, "last": last, "change_pct": (last / bars[0].open - 1) * 100,
            "high": hi, "low": lo, "range_pct": (hi / lo - 1) * 100 if lo else None,
            "volume": float(sum(c.volume_quote for c in raw)), "bars": len(bars), "zones": len(zones),
            "touches": len(touches),
            "held_pct": sum(t.result == "held" for t in decided) / len(decided) * 100 if decided else None,
            "pos_in_range_pct": (last - lo) / (hi - lo) * 100 if hi > lo else None,
        },
    )
    cache.set(key, result, CACHE_SECONDS)
    return result
