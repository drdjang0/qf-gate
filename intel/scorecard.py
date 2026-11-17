"""7.6 Brief scorecard and memory (idea from TradingAgents' decision-quality backtest + reflection).

A brief is settled once its horizon (default 24 h) has passed: the price at the first hourly candle that
opens at or after the due time, versus the price when the brief was made, and the same for BTC. Alpha is
the coin's return minus BTC's (for BTC itself: its plain return). Only briefs made live exist, so no LLM
"knows" the outcome it is judged on.

- Direction of a rating: Buy +1, Overweight +0.5, Hold 0, Underweight -0.5, Sell -1 (Review: not scored).
- A directional brief is a hit when its alpha has the rating's sign.
- Edge = average of direction x alpha over directional briefs: positive means following the briefs helped.
- Memory: a new brief for a coin is given the latest settled briefs for that coin and the overall record,
  only as known at its own start time.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from market.exchanges import public
from market.exchanges.base import ExchangeError

from .models import Brief

log = logging.getLogger(__name__)
SCORES = {"Buy": 1.0, "Overweight": 0.5, "Hold": 0.0, "Underweight": -0.5, "Sell": -1.0}


def price_at(exchange: str, base: str, quote: str, when) -> float | None:
    """Open of the first 1h candle opening at or after `when` (None if not there yet / not listed)."""
    start = when.replace(minute=0, second=0, microsecond=0)
    if start < when:
        start += timedelta(hours=1)
    try:
        rows = public.candles(exchange, base, quote, "1h", start, start + timedelta(hours=3))
    except ExchangeError:
        return None
    return float(rows[0].open) if rows else None


def _hour_ceil(t):
    s = t.replace(minute=0, second=0, microsecond=0)
    return s if s == t else s + timedelta(hours=1)


def check_invalidations(now=None, candles_fn=None) -> int:
    """7.9: record when an open brief's invalidation price was hit. Only closed 1h candles that open at or after
    the brief and before its due time are used (a brief made mid-hour skips that partial hour: no look-back)."""
    now = now or timezone.now()
    candles_fn = candles_fn or public.candles
    n = 0
    for b in Brief.objects.filter(status=Brief.Status.DONE, settled_at__isnull=True, invalidated_at__isnull=True):
        inv, d = b.invalidation, SCORES.get(b.rating)
        if inv is None or not d:
            continue
        start, end = _hour_ceil(b.created_at), min(now, b.due_at)
        if end - start < timedelta(hours=1):
            continue
        try:
            rows = candles_fn(b.exchange, b.symbol, b.quote, "1h", start, end) or []
        except ExchangeError:
            continue
        for r in rows:
            if r.open_time + timedelta(hours=1) > end:
                break  # not closed yet (or past the horizon)
            if (d > 0 and float(r.low) <= inv) or (d < 0 and float(r.high) >= inv):
                b.invalidated_at, b.invalidated_price = r.open_time, inv
                b.save(update_fields=["invalidated_at", "invalidated_price"])
                n += 1
                break
    return n


def settle_due(now=None, price_fn=None, candles_fn=None) -> int:
    """Settle every finished brief whose horizon has passed. Returns how many were settled."""
    now = now or timezone.now()
    price_fn = price_fn or price_at
    check_invalidations(now, candles_fn)  # final look before a brief closes
    n = 0
    for b in Brief.objects.filter(status=Brief.Status.DONE, settled_at__isnull=True, price__isnull=False):
        if b.due_at > now:
            continue
        p = price_fn(b.exchange, b.symbol, b.quote, b.due_at)
        if p is None:
            continue  # candle not closed yet, or the exchange is down: try again next round
        btc = p if b.symbol == "BTC" else price_fn("gate", "BTC", "USDT", b.due_at)
        b.settle_price, b.settled_at = p, now
        b.ret_pct = (p / b.price - 1) * 100
        if b.symbol == "BTC":
            b.btc_ret_pct, b.alpha_pct = b.ret_pct, b.ret_pct
        elif btc and b.btc_price:
            b.settle_btc_price = btc
            b.btc_ret_pct = (btc / b.btc_price - 1) * 100
            b.alpha_pct = b.ret_pct - b.btc_ret_pct
        b.save(update_fields=["settle_price", "settle_btc_price", "settled_at", "ret_pct", "btc_ret_pct", "alpha_pct"])
        n += 1
    if n:
        cache.delete("intel:filter_earned")
    return n


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def scorecard(as_of=None) -> dict:
    """Per-rating and per-coin record of settled briefs (as known at `as_of`)."""
    qs = Brief.objects.filter(status=Brief.Status.DONE, settled_at__isnull=False, alpha_pct__isnull=False)
    if as_of is not None:
        qs = qs.filter(settled_at__lte=as_of)
    briefs = [b for b in qs if b.rating in SCORES]
    rows = []
    for rating, d in SCORES.items():
        bs = [b for b in briefs if b.rating == rating]
        if not bs:
            continue
        hits = [((b.alpha_pct > 0) == (d > 0)) for b in bs] if d else []
        rows.append({"rating": rating, "n": len(bs), "avg_ret": _avg(b.ret_pct for b in bs),
                     "avg_alpha": _avg(b.alpha_pct for b in bs),
                     "hit_pct": sum(hits) / len(hits) * 100 if hits else None})
    directional = [b for b in briefs if SCORES[b.rating]]
    hits = [(b.alpha_pct > 0) == (SCORES[b.rating] > 0) for b in directional]
    coins: dict[str, list] = {}
    sources: dict[str, list] = {}
    for b in briefs:
        coins.setdefault(b.pair, []).append(b)
        sources.setdefault(b.source, []).append(b)
    labels = dict(Brief.Source.choices)
    with_inv = [b for b in directional if b.invalidation is not None]

    def _hit(bs):
        h = [(b.alpha_pct > 0) == (SCORES[b.rating] > 0) for b in bs if SCORES[b.rating]]
        return sum(h) / len(h) * 100 if h else None

    return {
        "settled": len(briefs), "directional": len(directional),
        "edge": _avg(SCORES[b.rating] * b.alpha_pct for b in directional),
        "hit_pct": sum(hits) / len(hits) * 100 if hits else None,
        # 7.9: how often the call's own "proven wrong" level was reached within the horizon
        "with_invalidation": len(with_inv),
        "invalidated": sum(1 for b in with_inv if b.invalidated_at),
        "invalidated_pct": (sum(1 for b in with_inv if b.invalidated_at) / len(with_inv) * 100) if with_inv else None,
        # 7.12: which entry point produces the useful briefs
        "by_source": sorted(({"source": k, "label": labels.get(k, k), "n": len(v), "avg_alpha": _avg(b.alpha_pct for b in v),
                              "edge": _avg(SCORES[b.rating] * b.alpha_pct for b in v if SCORES[b.rating]), "hit_pct": _hit(v)}
                             for k, v in sources.items()), key=lambda r: -r["n"]),
        "by_rating": rows,
        "by_coin": sorted(({"pair": k, "n": len(v), "avg_alpha": _avg(b.alpha_pct for b in v),
                            "edge": _avg(SCORES[b.rating] * b.alpha_pct for b in v if SCORES[b.rating])}
                           for k, v in coins.items()), key=lambda r: -r["n"]),
        "pending": Brief.objects.filter(status=Brief.Status.DONE, settled_at__isnull=True).count(),
    }


def filter_earned(as_of=None) -> dict:
    """7.7 gate: the AI filter may act only once enough settled briefs show a positive edge."""
    key = "intel:filter_earned"
    if as_of is None:
        hit = cache.get(key)
        if hit is not None:
            return hit
    s = scorecard(as_of)
    need = settings.INTEL["FILTER_MIN_BRIEFS"]
    ok = s["directional"] >= need and (s["edge"] or 0) > 0 and (s["hit_pct"] or 0) > 50
    why = ("earned" if ok else
           f"needs {need} settled directional briefs with a positive edge and a hit rate above 50% "
           f"(has {s['directional']}, edge {(s['edge'] or 0):+.2f}, hit rate {(s['hit_pct'] or 0):.0f}%)")
    out = {"ok": ok, "why": why, "directional": s["directional"], "edge": s["edge"], "hit_pct": s["hit_pct"]}
    if as_of is None:
        cache.set(key, out, 300)
    return out


def memory_for(symbol: str, quote: str, before) -> dict:
    """What earlier briefs taught us, as known at `before`: this coin's last settled briefs + the overall record."""
    past = Brief.objects.filter(symbol=symbol, quote=quote, status=Brief.Status.DONE, settled_at__isnull=False,
                                settled_at__lte=before, alpha_pct__isnull=False).order_by("-created_at")[:3]
    recent = []
    for b in past:
        d = SCORES.get(b.rating)
        verdict = "not scored" if not d else ("right" if (b.alpha_pct > 0) == (d > 0) else "wrong")
        recent.append({"date": b.created_at.strftime("%Y-%m-%d %H:%M"), "rating": b.rating, "ret_pct": round(b.ret_pct, 2),
                       "alpha_pct": round(b.alpha_pct, 2), "verdict": verdict,
                       "invalidation": b.invalidation, "invalidated": bool(b.invalidated_at)})
    s = scorecard(as_of=before)
    lessons = [f"{r['rating']} ratings so far: {r['n']} settled, average alpha vs BTC {r['avg_alpha']:+.2f}%"
               + (f", right {r['hit_pct']:.0f}% of the time" if r["hit_pct"] is not None else "")
               for r in s["by_rating"] if r["n"] >= 3]
    return {"recent": recent, "lessons": lessons}
