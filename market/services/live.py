"""Live market snapshot for the market page: order book, 24h ticker and recent trades.

Read straight from Gate.io's public REST API (nothing is stored). A 1-second
cache means several open tabs share one upstream request per second.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache

from ..exchanges.base import ExchangeClient

SNAPSHOT_TTL = 1  # seconds
PAIR_INFO_TTL = 3600
DEPTHS = (10, 20, 50)
DEFAULT_DEPTH = 20


def group_options(price_precision: int) -> list[str]:
    """Price-grouping steps offered for the book: the tick size and 10x, 100x, 1000x it."""
    out = []
    for k in range(4):
        step = Decimal(1).scaleb(-price_precision + k)
        out.append(format(step.normalize(), "f"))
    return out


def pair_info(client: ExchangeClient, pair: str) -> dict:
    key = f"live:pair:{pair}"
    info = cache.get(key)
    if info is None:
        info = client.pair_info(pair) or {"price_precision": 2, "amount_precision": 4}
        cache.set(key, info, PAIR_INFO_TTL)
    return info


def _side(levels: list[tuple[str, str]]) -> list[dict]:
    """Add the running total (cumulative amount from the best price outwards)."""
    out, total = [], Decimal(0)
    for price, amount in levels:
        total += Decimal(amount)
        out.append({"price": price, "amount": amount, "total": format(total.normalize(), "f")})
    return out


def snapshot(client: ExchangeClient, pair: str, depth: int = DEFAULT_DEPTH, group: str = "0") -> dict:
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    key = f"live:snap:{pair}:{depth}:{group}"
    data = cache.get(key)
    if data is not None:
        return data
    book = client.get_order_book(pair, limit=depth, group=group)
    asks, bids = _side(book["asks"]), _side(book["bids"])
    spread = None
    if asks and bids:
        a, b = Decimal(asks[0]["price"]), Decimal(bids[0]["price"])
        spread = {"abs": format((a - b).normalize(), "f"), "pct": float((a - b) / a * 100) if a else None}
    data = {
        "pair": pair,
        "book_time": book["time"],
        "asks": asks,
        "bids": bids,
        "spread": spread,
        "ticker": client.get_ticker(pair),
        "trades": client.get_trades(pair, limit=30),
    }
    cache.set(key, data, SNAPSHOT_TTL)
    return data
