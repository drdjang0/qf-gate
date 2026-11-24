"""Holdings, totals and the period report, built from ledger rows."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from . import pnl

D0 = Decimal(0)


def holdings(transactions, method: str, prices: dict[str, Decimal]) -> dict:
    """Per position: quantity, average cost, value at the live price, realised / unrealised P&L, fees."""
    books = pnl.compute(transactions, method)
    rows, totals = [], defaultdict(lambda: defaultdict(lambda: D0))
    for (market, asset, quote), b in sorted(books.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
        price = prices.get(f"{asset}_{quote}")
        value = b.qty * price if price is not None else None
        unreal = b.unrealised(price)
        rows.append({"market": market, "asset": asset, "quote": quote, "qty": b.qty, "avg_cost": b.avg_cost,
                     "cost": b.cost, "price": price, "value": value, "unrealised": unreal, "realised": b.realised,
                     "fees": b.fees, "flags": b.flags, "open": bool(b.qty), "first_at": b.first_at,
                     "last_at": b.last_at, "unmatched_cash": b.unmatched_cash})
        t = totals[quote]
        t["cost"] += b.cost
        t["realised"] += b.realised
        t["fees"] += b.fees
        if b.qty and price is None:
            t["unpriced"] += 1
        else:
            t["value"] += value or D0
            t["unrealised"] += unreal or D0
    for t in totals.values():
        t["total"] = t["realised"] + t["unrealised"]
    return {"rows": rows, "totals": {q: dict(t) for q, t in totals.items()}}


def period(transactions, method: str, start: datetime | None, end: datetime | None, asset: str = "") -> dict:
    """What was bought and sold between `start` and `end` (UTC), per coin, with the P&L realised in that time.

    Realised P&L needs the cost of what was sold, which may have been bought before the period, so the
    whole history is replayed and only the sales inside the period are counted.
    """
    txs = list(transactions)
    books = pnl.compute(txs, method)
    inside = [t for t in txs if (start is None or t.executed_at >= start) and (end is None or t.executed_at < end)
              and (not asset or t.base.upper() == asset.upper())]
    per = defaultdict(lambda: defaultdict(lambda: D0))
    for t in inside:
        key = (t.market, t.base.upper(), (t.quote or "").upper())
        r = per[key]
        if t.kind == "trade":
            amount = t.qty * (t.price or D0)
            fc = (t.fee_currency or "").upper()
            fee_base = t.fee if fc == key[1] else D0
            if t.side == "buy":
                r["buys"] += 1
                r["bought_qty"] += t.qty
                r["spent"] += amount
                r["net_qty"] += t.qty - fee_base
            else:
                r["sells"] += 1
                r["sold_qty"] += t.qty
                r["received"] += amount
                r["net_qty"] -= t.qty + fee_base
            r["fees"] += t.fee_quote if t.fee_quote is not None else D0
        elif t.kind == "deposit" or (t.kind == "adjustment" and t.side != "out"):
            r["in_qty"] += t.qty
            r["net_qty"] += t.qty
        else:
            r["out_qty"] += t.qty
            r["net_qty"] -= t.qty
    rows = []
    totals = defaultdict(lambda: defaultdict(lambda: D0))
    for key in sorted(per, key=lambda k: (k[2], k[1], k[0])):
        r = per[key]
        book = books.get(key)
        r["realised"] = book.realised_between(start, end) if book else D0
        r["avg_buy"] = r["spent"] / r["bought_qty"] if r["bought_qty"] else None
        r["avg_sell"] = r["received"] / r["sold_qty"] if r["sold_qty"] else None
        rows.append({"market": key[0], "asset": key[1], "quote": key[2], **r})
        for f in ("spent", "received", "fees", "realised"):
            totals[key[2]][f] += r[f]
    inside.sort(key=lambda t: (t.executed_at, t.id or 0))
    return {"rows": rows, "totals": {q: dict(t) for q, t in totals.items()}, "transactions": inside}
