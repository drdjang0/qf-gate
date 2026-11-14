"""Coin screener (idea from freqtrade's pair lists): filter every Gate.io spot pair by liquidity,
spread, volatility, price and recent performance, from one public "all tickers" request.

The result is a shortlist only: a coin is added through the normal form, which still verifies it by
chain + contract address.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache

from ..exchanges.gate import get_client

CACHE_SECONDS = 30


@dataclass
class Filters:
    quote: str = "USDT"
    min_volume: float = 1_000_000  # 24h volume in the quote currency
    max_spread_pct: float = 0.5
    min_range_pct: float = 0.0  # 24h (high - low) / low
    max_range_pct: float = 1000.0
    min_price: float = 0.0
    max_price: float = 0.0  # 0 = no limit
    min_change_pct: float = -100.0  # 24h change
    max_change_pct: float = 10_000.0
    blacklist: tuple[str, ...] = ()
    sort: str = "volume"  # volume | change | range | spread
    limit: int = 100


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rows_from_tickers(tickers: list[dict], quote: str) -> list[dict]:
    rows = []
    suffix = f"_{quote.upper()}"
    for t in tickers:
        pair = t.get("currency_pair", "")
        if not pair.endswith(suffix):
            continue
        last, bid, ask = _f(t.get("last")), _f(t.get("highest_bid")), _f(t.get("lowest_ask"))
        high, low = _f(t.get("high_24h")), _f(t.get("low_24h"))
        if not last or not bid or not ask or ask <= 0:
            continue
        rows.append({
            "pair": pair, "base": pair[: -len(suffix)], "last": last,
            "change_pct": _f(t.get("change_percentage")) or 0.0,
            "volume": _f(t.get("quote_volume")) or 0.0,
            "spread_pct": (ask - bid) / ask * 100,
            "range_pct": (high - low) / low * 100 if high and low else 0.0,
        })
    return rows


def screen(rows: list[dict], f: Filters) -> list[dict]:
    black = {b.strip().upper() for b in f.blacklist if b.strip()}
    out = [
        r for r in rows
        if r["base"] not in black
        and r["volume"] >= f.min_volume
        and r["spread_pct"] <= f.max_spread_pct
        and f.min_range_pct <= r["range_pct"] <= f.max_range_pct
        and r["last"] >= f.min_price and (not f.max_price or r["last"] <= f.max_price)
        and f.min_change_pct <= r["change_pct"] <= f.max_change_pct
    ]
    key = {"volume": lambda r: -r["volume"], "change": lambda r: -r["change_pct"],
           "range": lambda r: -r["range_pct"], "spread": lambda r: r["spread_pct"]}.get(f.sort, lambda r: -r["volume"])
    return sorted(out, key=key)[: f.limit]


def all_rows(quote: str) -> list[dict]:
    key = f"screener:{quote}"
    rows = cache.get(key)
    if rows is None:
        rows = rows_from_tickers(get_client().get_all_tickers(), quote)
        cache.set(key, rows, CACHE_SECONDS)
    return rows
