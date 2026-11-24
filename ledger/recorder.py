"""Copy QF-gate's own fills into the ledger: bots, paper trading and cascades.

Runs every minute in the worker (and before a Portfolio page renders). Each fill has a fixed
`source_key`, so running it again never records anything twice, and nothing in the trading code
needs to know the ledger exists. Rows stay in the ledger even if the bot / session / cascade that
made them is deleted later: the ledger is the permanent record.

Accounts:
- live bot orders  -> "Gate.io" (real), marked provisional until the Gate.io sync brings the exact trades
- dry-run bots and cascades -> "Dry-run" (simulated)
- paper trading -> "Paper" (simulated)
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction as db_tx

from .fields import to_dec
from .models import Account, Transaction

GATE, DRY_RUN, PAPER = "Gate.io", "Dry-run", "Paper"
DEFAULTS = {
    GATE: {"kind": Account.Kind.REAL, "exchange": "Gate.io", "sync": Account.Sync.GATE,
           "note": "Your real Gate.io spot account"},
    DRY_RUN: {"kind": Account.Kind.SIMULATED, "exchange": "Gate.io (simulated)",
              "note": "Dry-run bots and cascades: simulated fills on real prices"},
    PAPER: {"kind": Account.Kind.SIMULATED, "exchange": "Simulated", "note": "Paper-trading sessions"},
}


def account(name: str) -> Account:
    acc, _ = Account.objects.get_or_create(name=name, defaults=DEFAULTS.get(name, {}))
    return acc


def _d(x) -> Decimal | None:
    return to_dec(x) if x is not None else None


def _known(prefix: str) -> set[str]:
    return set(Transaction.objects.filter(source_key__startswith=prefix).values_list("source_key", flat=True))


def _base(coin) -> str:
    return (coin.gate_currency or coin.symbol).upper()


def record_bots() -> int:
    from trading.models import BotOrder

    known = _known("botorder:")
    gate = dry = None
    synced = set()
    rows = []
    qs = (BotOrder.objects.filter(status=BotOrder.Status.FILLED, filled_qty__gt=0)
          .select_related("bot__config__coin").order_by("id"))
    for o in qs:
        key = f"botorder:{o.pk}"
        if key in known:
            continue
        bot, coin = o.bot, o.bot.config.coin
        live = bot.mode == "live"
        if live:
            gate = gate or account(GATE)
            if not synced:
                synced = set(Transaction.objects.filter(account=gate, provisional=False)
                             .exclude(order_id="").values_list("order_id", flat=True)) or {""}
            if o.exchange_id and o.exchange_id in synced:
                continue  # Gate.io's exact trades for this order are already in
            acc = gate
        else:
            dry = dry or account(DRY_RUN)
            acc = dry
        rows.append(Transaction(
            account=acc, kind="trade", market="spot", base=_base(coin), quote=coin.quote.upper(), coin=coin,
            side=o.side, qty=_d(o.filled_qty), price=_d(o.avg_price), fee=_d(o.fee) or Decimal(0),
            fee_currency=coin.quote.upper(), fee_quote=_d(o.fee) or Decimal(0), executed_at=o.created_at,
            route="bot", source=f"Bot #{bot.pk} · {bot.config.name}"[:120], source_key=key,
            order_id=o.exchange_id or "", provisional=live,
            raw={"bot_order": o.pk, "bot": bot.pk, "mode": bot.mode, "reason": o.reason, "ref_price": o.ref_price,
                 "requested_qty": o.qty, "pnl": o.pnl},
            note="Waiting for Gate.io's exact trades (sync)" if live else ""))
    Transaction.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def record_paper() -> int:
    from trading.models import Trade

    known = _known("papertrade:")
    acc = None
    rows = []
    for t in Trade.objects.filter(paper__isnull=False).select_related("paper__config__coin").order_by("id"):
        k_open, k_close = f"papertrade:{t.pk}:open", f"papertrade:{t.pk}:close"
        if k_open in known and k_close in known:
            continue
        acc = acc or account(PAPER)
        coin, cfg = t.paper.config.coin, t.paper.config
        short = t.side == "short"
        qty, entry, exit_ = _d(t.qty), _d(t.entry_price), _d(t.exit_price)
        direction = -1 if short else 1
        # everything between the round trip's price move and its recorded P&L: fees of both legs (+ funding)
        costs = (exit_ - entry) * qty * direction - _d(t.pnl)
        common = dict(account=acc, kind="trade", market="margin" if short else "spot", base=_base(coin),
                      quote=coin.quote.upper(), coin=coin, qty=qty, route="paper",
                      source=f"Paper #{t.paper_id} · {cfg.name}"[:120])
        rows.append(Transaction(**common, side="sell" if short else "buy", price=entry, fee=Decimal(0),
                                executed_at=t.entry_time, source_key=k_open,
                                raw={"trade": t.pk, "leg": "open", "reason": t.entry_reason}))
        rows.append(Transaction(**common, side="buy" if short else "sell", price=exit_, fee=costs,
                                fee_currency=coin.quote.upper(), fee_quote=costs, executed_at=t.exit_time,
                                source_key=k_close, note="Fee = both legs' fees (and funding) of this round trip",
                                raw={"trade": t.pk, "leg": "close", "reason": t.exit_reason, "pnl": t.pnl,
                                     "fees": t.fees}))
    Transaction.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def record_cascades() -> int:
    from cascades.models import CascadeLevel

    known = _known("cascade:")
    acc = None
    rows = []
    qs = CascadeLevel.objects.filter(filled_at__isnull=False).select_related("cascade__coin").order_by("id")
    for lv in qs:
        c, coin = lv.cascade, lv.cascade.coin
        common = dict(kind="trade", market="spot", base=_base(coin), quote=coin.quote.upper(), coin=coin,
                      route="cascade", source=f"Cascade #{c.pk}{' · ' + c.name if c.name else ''}"[:120],
                      fee_currency=coin.quote.upper())
        k_buy, k_sell = f"cascade:{lv.pk}:buy", f"cascade:{lv.pk}:sell"
        if k_buy not in known and lv.qty:
            acc = acc or account(DRY_RUN)
            # qty is what was received; the fee (taken off what was received) is booked in the quote currency
            rows.append(Transaction(**common, account=acc, side="buy", qty=_d(lv.qty), price=_d(lv.fill_price),
                                    fee=_d(lv.buy_fee), fee_quote=_d(lv.buy_fee), executed_at=lv.filled_at,
                                    source_key=k_buy, raw={"level": lv.pk, "index": lv.index,
                                                           "amount_usdt": lv.amount_usdt}))
        if k_sell not in known and lv.sell_status == "filled" and lv.sell_filled_at:
            acc = acc or account(DRY_RUN)
            rows.append(Transaction(**common, account=acc, side="sell", qty=_d(lv.qty), price=_d(lv.sell_target),
                                    fee=_d(lv.sell_fee), fee_quote=_d(lv.sell_fee), executed_at=lv.sell_filled_at,
                                    source_key=k_sell, raw={"level": lv.pk, "index": lv.index, "pnl": lv.pnl}))
    Transaction.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def record_internal() -> int:
    """Bring every new QF-gate fill into the ledger. Returns the number of rows added."""
    with db_tx.atomic():
        return record_bots() + record_paper() + record_cascades()


def ensure_accounts() -> None:
    for name in (GATE, DRY_RUN, PAPER):
        account(name)
