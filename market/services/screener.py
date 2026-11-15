"""Coin screener (idea from freqtrade's pair lists): filter every spot pair by liquidity, spread,
volatility, price and recent performance, from one public "all tickers" request per exchange.

Pick one exchange (Gate.io, Binance, OKX, Bybit, KuCoin, MEXC, Bitget) or "all exchanges combined":
pairs are merged by ticker, volume is summed, and the price/spread/change/range come from the venue
with the most volume. Tickers are not identities, so a big price gap between venues is flagged (it is
often a different coin sharing a ticker).

The result is a shortlist only: a coin is added (on Gate.io) through the normal form, which still
verifies it by chain + contract address.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.core.cache import cache

from ..exchanges import public
from ..exchanges.base import ExchangeError
from ..exchanges.gate import get_client

CACHE_SECONDS = 30
ALL = "all"
GAP_WARN_PCT = 5.0  # venues disagree by more than this: possibly different coins with one ticker


def exchange_choices() -> list[tuple[str, str]]:
    return [(k, e.name) for k, e in public.EXCHANGES.items()] + [(ALL, "All exchanges (combined)")]


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
    sort: str = "volume"  # volume | change | range | spread | venues
    limit: int = 100
    min_venues: int = 1  # combined mode: listed on at least this many exchanges


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
        and r.get("venue_count", 1) >= f.min_venues
    ]
    key = {"volume": lambda r: -r["volume"], "change": lambda r: -r["change_pct"],
           "range": lambda r: -r["range_pct"], "spread": lambda r: r["spread_pct"],
           "venues": lambda r: (-r.get("venue_count", 1), -r["volume"])}.get(f.sort, lambda r: -r["volume"])
    return sorted(out, key=key)[: f.limit]


def all_rows(quote: str) -> list[dict]:
    key = f"screener:{quote}"
    rows = cache.get(key)
    if rows is None:
        rows = rows_from_tickers(get_client().get_all_tickers(), quote)
        cache.set(key, rows, CACHE_SECONDS)
    return rows


def rows_from_public(ticks: list[dict], exchange: str, quote: str) -> list[dict]:
    """Normalised public tickers (see exchanges.public.all_tickers) -> screener rows."""
    sep = {"gate": "_", "okx": "-", "kucoin": "-"}.get(exchange, "")
    rows = []
    for t in ticks:
        last, bid, ask, high, low = t["last"], t["bid"], t["ask"], t["high"], t["low"]
        if not last or not bid or not ask or ask <= 0:
            continue
        rows.append({
            "pair": f"{t['base']}{sep}{quote}", "base": t["base"], "last": last, "change_pct": t["change_pct"],
            "volume": t["quote_volume"], "spread_pct": (ask - bid) / ask * 100,
            "range_pct": (high - low) / low * 100 if high and low else 0.0,
        })
    return rows


def exchange_rows(exchange: str, quote: str) -> list[dict]:
    """All `quote` pairs on one exchange (cached). Raises ExchangeError if it can't be reached."""
    if exchange == "gate":
        rows = all_rows(quote)
    else:
        key = f"screener:{exchange}:{quote}"
        rows = cache.get(key)
        if rows is None:
            rows = rows_from_public(public.all_tickers(exchange, quote), exchange, quote)
            cache.set(key, rows, CACHE_SECONDS)
    for r in rows:
        r["exchange"] = exchange
    return rows


def combine(per_exchange: dict[str, list[dict]]) -> list[dict]:
    """Merge rows from several exchanges by ticker (base). The venue with the most volume leads."""
    by_base: dict[str, list[dict]] = {}
    for ex, rows in per_exchange.items():
        for r in rows:
            by_base.setdefault(r["base"], []).append({**r, "exchange": ex})
    out = []
    for base, venues in by_base.items():
        venues.sort(key=lambda r: -r["volume"])
        lead = venues[0]
        total = sum(v["volume"] for v in venues)
        prices = [v["last"] for v in venues if v["last"]]
        gap = (max(prices) / min(prices) - 1) * 100 if len(prices) > 1 and min(prices) > 0 else 0.0
        out.append({
            **lead, "volume": total, "lead": lead["exchange"], "lead_volume": lead["volume"],
            "venue_count": len(venues), "price_gap_pct": gap, "gap_warn": gap > GAP_WARN_PCT,
            "venues": [{"key": v["exchange"], "name": public.EXCHANGES[v["exchange"]].name,
                        "share_pct": v["volume"] / total * 100 if total else 0.0, "last": v["last"]} for v in venues],
        })
    return out


def combined_rows(quote: str) -> tuple[list[dict], dict[str, str]]:
    """Every exchange in parallel, merged. Unreachable exchanges are skipped and reported."""
    def one(key):
        try:
            return key, exchange_rows(key, quote), ""
        except ExchangeError as exc:
            return key, None, str(exc)

    with ThreadPoolExecutor(max_workers=len(public.EXCHANGES)) as pool:
        results = list(pool.map(one, public.EXCHANGES))
    errors = {public.EXCHANGES[k].name: e for k, _rows, e in results if e}
    return combine({k: rows for k, rows, _e in results if rows is not None}), errors


def gate_bases(quote: str) -> set[str] | None:
    """Tickers tradable on Gate.io (where coins are added); None if Gate.io can't be reached."""
    try:
        return {r["base"] for r in all_rows(quote)}
    except ExchangeError:
        return None
