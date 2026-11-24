"""Holdings and profit / loss from ledger rows, by average cost (default) or FIFO.

Pure functions over `Transaction`-like objects, all in exact `Decimal`.

How a row moves a position (one position per market + coin + quote currency):
- buy:  +qty received (minus a fee taken in the coin), paying qty x price (+ a fee in the quote currency).
- sell: -qty delivered, receiving qty x price (- a fee in the quote currency).
- A fee paid in a third coin (GT) counts as selling that coin at the fee's value, so GT holdings and
  their P&L stay right; Gate points are not a balance and only count as a fee.
- deposit / adjustment in: +qty at a cost of qty x price (cost unknown when there is no price: flagged).
- withdrawal / adjustment out: -qty, its cost basis leaves with it (no profit or loss).

Average cost: one pooled cost; a sale realises proceeds - average cost x qty.
FIFO: a list of lots; a sale uses the oldest lots first.
Spot positions can't go below zero: a sale larger than what the ledger holds means history is missing;
that part is kept apart as "unmatched" instead of inventing a short. Margin and futures allow shorts.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

D0 = Decimal(0)
METHODS = {"avg": "Average cost", "fifo": "FIFO (oldest first)"}
DEFAULT_METHOD = "avg"
NO_BALANCE = {"POINT", "POINTS"}  # Gate points pay fees but are not a spot balance
CTX = decimal.Context(prec=40)


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


@dataclass
class Effect:
    at: datetime
    key: tuple  # (market, asset, quote)
    dq: Decimal  # signed quantity change
    cash: Decimal | None  # quote paid (+) / received (-); None for transfers
    kind: str  # trade | in | out
    tx_id: int | None = None
    basis_price: Decimal | None = None  # for "in"
    fee: Decimal = D0  # fee value in the quote currency charged to this position


def effects(tx) -> list[Effect]:
    """What one ledger row does to positions."""
    out: list[Effect] = []
    market, base, quote = tx.market, tx.base.upper(), (tx.quote or "").upper()
    key = (market, base, quote)
    fee, fc = tx.fee or D0, (tx.fee_currency or "").upper()
    if tx.kind == "trade":
        price = tx.price or D0
        gross = tx.qty * price
        fee_in_base = fee if fc == base else D0
        fee_in_quote = fee if fc == quote else D0
        if fc == base:
            fee_value = fee * price
        elif fc == quote:
            fee_value = fee
        else:
            fee_value = (tx.fee_quote or D0) if fee else D0
        third = fee_value if fee and fc not in (base, quote) else D0
        if tx.side == "buy":
            out.append(Effect(tx.executed_at, key, tx.qty - fee_in_base, gross + fee_in_quote + third, "trade",
                              tx.id, fee=fee_value))
        else:
            out.append(Effect(tx.executed_at, key, -(tx.qty + fee_in_base), -(gross - fee_in_quote - third), "trade",
                              tx.id, fee=fee_value))
        if fee and fc and fc not in (base, quote) and fc not in NO_BALANCE:
            # paid with another coin (GT): that coin is spent at the fee's value
            known = tx.fee_quote is not None
            out.append(Effect(tx.executed_at, ("spot", fc, quote or "USDT"), -fee,
                              -tx.fee_quote if known else None, "trade" if known else "out", tx.id))
    elif tx.kind in ("deposit", "withdrawal", "adjustment"):
        incoming = tx.kind == "deposit" or (tx.kind == "adjustment" and tx.side != "out")
        qty = tx.qty
        if not incoming and fc == base:
            qty += fee  # a withdrawal fee taken in the coin leaves the account too
        out.append(Effect(tx.executed_at, key, qty if incoming else -qty, None, "in" if incoming else "out",
                          tx.id, basis_price=tx.price))
    return out


@dataclass
class Lot:
    qty: Decimal
    cost: Decimal
    at: datetime | None = None


@dataclass
class Book:
    """One position: lots, realised P&L and the flags that say how far to trust it."""

    market: str
    asset: str
    quote: str
    method: str = DEFAULT_METHOD
    lots: list = field(default_factory=list)
    realised: Decimal = D0
    events: list = field(default_factory=list)  # (at, realised amount, tx_id)
    fees: Decimal = D0
    unknown_cost_qty: Decimal = D0  # came in with no cost (deposit without a price)
    unmatched_qty: Decimal = D0  # sold / sent more than the ledger holds: history missing
    unmatched_cash: Decimal = D0
    transferred_out_cost: Decimal = D0
    first_at: datetime | None = None
    last_at: datetime | None = None

    @property
    def allow_short(self) -> bool:
        return self.market != "spot"

    @property
    def qty(self) -> Decimal:
        return sum((lot.qty for lot in self.lots), D0)

    @property
    def cost(self) -> Decimal:
        return sum((lot.cost for lot in self.lots), D0)

    @property
    def avg_cost(self) -> Decimal | None:
        q = self.qty
        return CTX.divide(self.cost, q) if q else None

    def unrealised(self, price: Decimal | None) -> Decimal | None:
        if price is None:
            return None
        return self.qty * price - self.cost if self.lots else D0

    def _open(self, q: Decimal, cash: Decimal, at) -> None:
        if self.method == "avg" and self.lots:
            self.lots[0].qty += q
            self.lots[0].cost += cash
        else:
            self.lots.append(Lot(q, cash, at))

    def _close(self, q: Decimal, cash: Decimal | None) -> tuple[Decimal, Decimal | None, Decimal]:
        """Use lots of the opposite sign. Returns (quantity left over, cash left over, realised)."""
        realised = D0
        while q and self.lots and _sign(self.lots[0].qty) != _sign(q):
            lot = self.lots[0]
            take = min(abs(q), abs(lot.qty))
            basis = CTX.divide(lot.cost * take, abs(lot.qty))
            if cash is not None:
                part = CTX.divide(cash * take, abs(q))
                realised += -part - basis
                cash -= part
            else:
                self.transferred_out_cost += basis
            lot.qty += _sign(q) * take
            lot.cost -= basis
            if not lot.qty:
                self.lots.pop(0)
            q -= _sign(q) * take
        return q, cash, realised

    def apply(self, ev: Effect) -> None:
        self.first_at = self.first_at or ev.at
        self.last_at = ev.at
        self.fees += ev.fee
        if ev.kind == "in":
            if ev.basis_price is None:
                self.unknown_cost_qty += ev.dq
            cost = ev.dq * ev.basis_price if ev.basis_price is not None else D0
            left, _, _ = self._close(ev.dq, None)  # covers a short first (margin), then opens
            if left:
                self._open(left, CTX.divide(cost * left, ev.dq), ev.at)
            return
        q, cash, realised = self._close(ev.dq, ev.cash)
        if realised:
            self.realised += realised
            self.events.append((ev.at, realised, ev.tx_id))
        if not q:
            return
        if q < 0 and not self.allow_short:
            self.unmatched_qty += -q
            if cash is not None:
                self.unmatched_cash += -cash
            return
        self._open(q, cash if cash is not None else D0, ev.at)

    @property
    def flags(self) -> list[str]:
        f = []
        if self.unknown_cost_qty:
            f.append(f"{self.unknown_cost_qty.normalize():f} came in with no cost price")
        if self.unmatched_qty:
            f.append(f"{self.unmatched_qty.normalize():f} sold or sent beyond what the ledger holds (history missing)")
        return f

    def realised_between(self, start=None, end=None) -> Decimal:
        return sum((amt for at, amt, _ in self.events
                    if (start is None or at >= start) and (end is None or at < end)), D0)


def compute(transactions, method: str = DEFAULT_METHOD) -> dict[tuple, Book]:
    """Replay rows oldest first. Returns {(market, asset, quote): Book}."""
    method = method if method in METHODS else DEFAULT_METHOD
    books: dict[tuple, Book] = {}
    for tx in sorted(transactions, key=lambda t: (t.executed_at, t.id or 0)):
        for ev in effects(tx):
            if not ev.key[1] or ev.key[1] == ev.key[2]:
                continue  # the quote currency itself is cash, not a position
            book = books.get(ev.key)
            if book is None:
                book = books[ev.key] = Book(*ev.key, method=method)
            book.apply(ev)
    return books


def balances(transactions) -> dict[str, Decimal]:
    """Net amount of every currency the rows put in or took out of a spot account (for balance checks)."""
    bal: dict[str, Decimal] = {}

    def add(cur: str, amount: Decimal) -> None:
        cur = (cur or "").upper()
        if cur and cur not in NO_BALANCE:
            bal[cur] = bal.get(cur, D0) + amount

    for tx in transactions:
        if tx.market != "spot":
            continue
        fee, fc = tx.fee or D0, tx.fee_currency or ""
        if tx.kind == "trade":
            s = 1 if tx.side == "buy" else -1
            add(tx.base, s * tx.qty)
            add(tx.quote, -s * tx.qty * (tx.price or D0))
            if fee and fc:
                add(fc, -fee)
        else:
            incoming = tx.kind == "deposit" or (tx.kind == "adjustment" and tx.side != "out")
            add(tx.base, tx.qty if incoming else -tx.qty)
            if not incoming and fee and fc:
                add(fc, -fee)
    return bal
