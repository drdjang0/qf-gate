"""Dry-run cascade engine: simulated limit orders on Gate.io's real prices. Nothing is sent to the exchange.

Each step looks at the live price plus the stored 1-minute candles since an order was placed, so a wick
between two steps still fills a resting order (the same low / high rules as the backtester):

1. waiting for the trigger: arm once the price since `watch_from` has reached it;
2. a resting buy fills (at its limit price) once the price has traded down to it;
3. a resting sell fills once the price has traded up to it (never in the step that placed it);
4. every bought level without a sell gets its take-profit sell (fill price x (1 + tp %));
5. when no buy is resting, the next level is chosen with skip-ahead on the current price; at most one
   buy rests at a time, which is what stops a fast crash from deploying the whole ladder at once;
6. done when every level is sold, skipped or cancelled.

The worker steps every running cascade every 30 s (safety net) and the live stream steps a coin's
cascades on its price ticks. A compare-and-set version keeps the two from applying the same fill twice.
The kill switch (Control page) stops new buys; sells keep working.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from market.exchanges.base import ExchangeError
from market.models import Candle, Coin

from . import ladder
from .models import Cascade, CascadeEvent, CascadeLevel

log = logging.getLogger(__name__)

RUNNING = (Cascade.Status.PENDING_TRIGGER, Cascade.Status.ACTIVE)
L, S = CascadeLevel.Status, CascadeLevel.SellStatus
PRICE_RATIO = (0.2, 3.0)  # level 1 buy price vs the market: outside this looks like a misplaced decimal
TICK_THROTTLE_SECONDS = 2
HALT_NOTE = "Kill switch is on: no new buys (sells keep working)"
# the fields the engine decides; live order ids belong to cascades/live.py, so the engine never overwrites them
ENGINE_FIELDS = ["status", "skip_reason", "skip_price", "placed_at", "filled_at", "fill_price", "qty", "buy_fee",
                 "sell_status", "sell_target", "sell_placed_at", "sell_filled_at", "sell_fee", "pnl", "target_price",
                 "amount_usdt", "take_profit_pct"]


class Conflict(Exception):
    """Someone else changed the cascade between our read and our write; nothing was applied."""


def fmt(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:,.2f}" if abs(x) >= 100 else f"{x:.8g}"


def live_price(coin: Coin) -> float | None:
    from market.exchanges.gate import get_client

    try:
        t = get_client().get_ticker(coin.pair) or {}
    except ExchangeError:
        return None
    try:
        p = float(t.get("last") or 0)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def _ceil_minute(dt: datetime) -> datetime:
    floor = dt.replace(second=0, microsecond=0)
    return floor if floor == dt else floor + timedelta(minutes=1)


def window(coin: Coin, since: datetime, price: float, now: datetime) -> tuple[float, float]:
    """Lowest and highest price from `since` to now: the live price plus the 1-minute candles that opened
    after `since` (a candle that opened before it may hold prices from before the order existed)."""
    agg = Candle.objects.filter(coin=coin, interval="1m", open_time__gte=_ceil_minute(since),
                                open_time__lte=now).aggregate(lo=Min("low"), hi=Max("high"))
    lo = min(price, float(agg["lo"])) if agg["lo"] is not None else price
    hi = max(price, float(agg["hi"])) if agg["hi"] is not None else price
    return lo, hi


def check_price(levels: list[dict], price: float | None) -> None:
    """Gate won't reject a valid-but-wrong limit price (a misplaced decimal just fills at a bad price)."""
    if price and levels:
        ratio = levels[0]["target_price"] / price
        if not PRICE_RATIO[0] <= ratio <= PRICE_RATIO[1]:
            raise ValueError(f"Level 1 buy price ({fmt(levels[0]['target_price'])}) is far from the market price "
                             f"({fmt(price)}). Check for a misplaced decimal.")


# -- create -------------------------------------------------------------------------------------------


def create(coin: Coin, kind: str, levels: list[dict], *, trigger_price: float | None = None, fee_pct: float = 0.2,
           config: dict | None = None, name: str = "", price: float | None = None,
           now: datetime | None = None, owner=None, mode: str = "dry_run") -> Cascade:
    """Save a cascade and its levels; with a price, run the first step at once (place level 1)."""
    now = now or timezone.now()
    check_price(levels, price)
    if mode == Cascade.Mode.LIVE:
        from trading.brokers import live_allowed

        ok, missing = live_allowed(owner)
        if not ok:
            raise ValueError("Live trading is not allowed: " + "; ".join(missing))
    with transaction.atomic():
        c = Cascade.objects.create(
            owner=owner, mode=mode, coin=coin, kind=kind, name=name[:100], config=config or {}, trigger_price=trigger_price, fee_pct=fee_pct,
            status=Cascade.Status.PENDING_TRIGGER if trigger_price else Cascade.Status.ACTIVE, watch_from=now,
        )
        CascadeLevel.objects.bulk_create([CascadeLevel(cascade=c, index=lv["index"], target_price=lv["target_price"],
                                                       amount_usdt=lv["amount_usdt"], take_profit_pct=lv["take_profit_pct"])
                                          for lv in levels])
        total = sum(lv["amount_usdt"] for lv in levels)
        CascadeEvent.objects.create(cascade=c, at=now, kind="created", price=price, message=(
            f"{len(levels)} levels, {fmt(total)} {coin.quote} in total"
            + (f", waiting for {fmt(trigger_price)}" if trigger_price else "")))
    if price:
        step(c.pk, price, now)
    return c


# -- the step -----------------------------------------------------------------------------------------


def step(cascade_id: int, price: float, now: datetime | None = None) -> int:
    """One pass over a cascade at `price`. Returns how many events it recorded (0 on a write conflict)."""
    now = now or timezone.now()
    try:
        with transaction.atomic():
            n = _step(cascade_id, float(price), now)
    except Conflict:
        return 0
    if Cascade.objects.filter(pk=cascade_id, mode=Cascade.Mode.LIVE).exists():
        from . import live

        n += live.reconcile(cascade_id, price=float(price))
    return n


def _step(pk: int, price: float, now: datetime) -> int:
    from trading.models import TradingControl

    c = Cascade.objects.select_related("coin").get(pk=pk)
    if not c.is_running:
        return 0
    version = c.version
    events: list[CascadeEvent] = []

    def ev(kind: str, msg: str, level: int | None = None, p: float | None = None) -> None:
        events.append(CascadeEvent(cascade=c, at=now, level=level, kind=kind, price=p, message=msg[:255]))

    levels = list(c.levels.order_by("index"))
    dirty: dict[int, CascadeLevel] = {}
    fee = c.fee_pct / 100
    note = ""

    if c.status == Cascade.Status.PENDING_TRIGGER:
        lo, _ = window(c.coin, c.watch_from, price, now)
        if c.trigger_price is None or lo <= c.trigger_price:
            c.status = Cascade.Status.ACTIVE
            ev("armed", f"Price reached the trigger {fmt(c.trigger_price)}: the cascade is active", p=price)

    simulated = c.mode != Cascade.Mode.LIVE
    if c.status == Cascade.Status.ACTIVE:
        for lv in levels:  # resting buy filled? (dry-run: from prices; live: Gate.io reports it, see live.py)
            if simulated and lv.status == L.PLACED and lv.placed_at:
                lo, _ = window(c.coin, lv.placed_at, price, now)
                if lo <= lv.target_price:
                    lv.status, lv.fill_price, lv.filled_at = L.FILLED, lv.target_price, now
                    lv.qty = lv.amount_usdt / lv.target_price * (1 - fee)
                    lv.buy_fee = lv.amount_usdt * fee
                    dirty[lv.pk] = lv
                    ev("bought", f"Bought {fmt(lv.qty)} for {fmt(lv.amount_usdt)} {c.coin.quote} at {fmt(lv.fill_price)}",
                       lv.index, lv.fill_price)
        for lv in levels:  # resting sell filled? (never in the step that placed it)
            if (simulated and lv.status == L.FILLED and lv.sell_status == S.PLACED and lv.sell_placed_at
                    and lv.sell_placed_at < now):
                _, hi = window(c.coin, lv.sell_placed_at, price, now)
                if hi >= lv.sell_target:
                    gross = (lv.qty or 0) * lv.sell_target
                    lv.sell_status, lv.sell_filled_at = S.FILLED, now
                    lv.sell_fee = gross * fee
                    lv.pnl = gross - lv.sell_fee - lv.amount_usdt
                    dirty[lv.pk] = lv
                    ev("sold", f"Sold at {fmt(lv.sell_target)}: profit {fmt(lv.pnl)} {c.coin.quote}", lv.index, lv.sell_target)
        for lv in levels:  # every bought level gets its own take-profit sell
            if lv.status == L.FILLED and lv.sell_status == S.NONE:
                lv.sell_target = lv.sell_target or ladder.sell_target(lv.fill_price, lv.take_profit_pct)
                lv.sell_status, lv.sell_placed_at = S.PLACED, now
                dirty[lv.pk] = lv
                ev("sell_placed", f"Take-profit sell at {fmt(lv.sell_target)} (+{lv.take_profit_pct:.2f}%)",
                   lv.index, lv.sell_target)

        resting = any(lv.status == L.PLACED for lv in levels)
        pending = [lv for lv in levels if lv.status == L.PENDING]
        if pending and not resting:
            if TradingControl.for_user(c.owner if c.owner_id else None).halted:
                note = HALT_NOTE
                if c.last_error != HALT_NOTE:
                    ev("blocked", HALT_NOTE)
            else:
                place, skip = ladder.select_next([lv.as_dict() for lv in pending], price)
                by_pk = {lv.pk: lv for lv in pending}
                for d in skip:
                    lv = by_pk[d["pk"]]
                    lv.status, lv.skip_reason, lv.skip_price = L.SKIPPED, "market already at or below the buy price", price
                    dirty[lv.pk] = lv
                    ev("skipped", f"Skipped: market {fmt(price)} is already at or below {fmt(lv.target_price)}", lv.index, price)
                if place:
                    lv = by_pk[place["pk"]]
                    lv.status, lv.placed_at = L.PLACED, now
                    dirty[lv.pk] = lv
                    ev("placed", f"Buy {fmt(lv.amount_usdt)} {c.coin.quote} resting at {fmt(lv.target_price)}"
                       + (" (last level: placed even though the market is already below it)" if lv.target_price >= price else ""),
                       lv.index, lv.target_price)

        if levels and all(lv.status in (L.SKIPPED, L.CANCELLED) or (lv.status == L.FILLED and lv.sell_status == S.FILLED)
                          for lv in levels):
            c.status = Cascade.Status.COMPLETED
            ev("completed", f"Every level is done. Realised P&L {fmt(sum(lv.pnl or 0 for lv in levels))} {c.coin.quote}")

    if not events and not dirty:
        Cascade.objects.filter(pk=c.pk).update(last_price=price, last_checked_at=now, last_error=note)
        return 0
    for lv in dirty.values():
        lv.save(update_fields=ENGINE_FIELDS)
    if not Cascade.objects.filter(pk=c.pk, version=version).update(
            version=version + 1, status=c.status, last_price=price, last_checked_at=now, last_error=note, updated_at=now):
        raise Conflict
    CascadeEvent.objects.bulk_create(events)
    return len(events)


def step_all() -> int:
    """Worker periodic: step every running cascade at its coin's live price (one ticker call per coin)."""
    by_coin: dict[int, list[Cascade]] = defaultdict(list)
    for c in Cascade.objects.filter(status__in=RUNNING).select_related("coin"):
        by_coin[c.coin_id].append(c)
    n = 0
    for cs in by_coin.values():
        price = live_price(cs[0].coin)
        if price is None:
            Cascade.objects.filter(pk__in=[c.pk for c in cs]).update(last_error="No live price from Gate.io")
            continue
        for c in cs:
            try:
                n += step(c.pk, price)
            except Exception as exc:  # noqa: BLE001 - one cascade must not stop the others
                log.exception("Cascade %s step failed", c.pk)
                Cascade.objects.filter(pk=c.pk).update(last_error=f"{exc.__class__.__name__}: {exc}"[:2000])
    from . import live

    n += live.reconcile_idle()
    return n


def on_price(coin: Coin, price: float) -> int:
    """Live stream tick: step this coin's running cascades (at most once every few seconds each)."""
    n = 0
    for pk in Cascade.objects.filter(status__in=RUNNING, coin=coin).values_list("pk", flat=True):
        if not cache.add(f"cascade:tick:{pk}", 1, TICK_THROTTLE_SECONDS):
            continue
        try:
            n += step(pk, price)
        except Exception:  # noqa: BLE001 - the stream must keep running
            log.exception("Cascade %s price step failed", pk)
    return n


# -- user actions -------------------------------------------------------------------------------------


def _reconcile_if_live(cascade: Cascade) -> None:
    if Cascade.objects.filter(pk=cascade.pk, mode=Cascade.Mode.LIVE).exists():
        from . import live

        live.reconcile(cascade.pk)


def _mutate(cascade: Cascade, fn) -> None:
    """Apply `fn(fresh_cascade) -> [(kind, message, level)]` under the same compare-and-set as the step."""
    with transaction.atomic():
        c = Cascade.objects.select_related("coin").get(pk=cascade.pk)
        version = c.version
        notes = fn(c) or []
        now = timezone.now()
        if not Cascade.objects.filter(pk=c.pk, version=version).update(
                version=version + 1, status=c.status, held_from=c.held_from, watch_from=c.watch_from, updated_at=now):
            raise Conflict("The cascade changed at the same moment (a fill?). Look again and retry.")
        CascadeEvent.objects.bulk_create([CascadeEvent(cascade=c, at=now, kind=k, message=m[:255], level=lv)
                                          for k, m, lv in notes])


def hold(cascade: Cascade) -> None:
    """Pause: the resting buy goes back to pending and resting sells are withdrawn (re-placed on resume)."""
    def fn(c):
        if not c.is_running:
            raise ValueError("Only a running cascade can be put on hold")
        c.held_from, c.status = c.status, Cascade.Status.HELD
        c.levels.filter(status=L.PLACED).update(status=L.PENDING, placed_at=None)
        c.levels.filter(status=L.FILLED, sell_status=S.PLACED).update(sell_status=S.NONE, sell_placed_at=None)
        return [("held", "On hold: resting orders withdrawn", None)]
    _mutate(cascade, fn)
    _reconcile_if_live(cascade)


def resume(cascade: Cascade, price: float | None = None) -> None:
    def fn(c):
        if c.status != Cascade.Status.HELD:
            raise ValueError("Only a cascade on hold can be resumed")
        c.status = c.held_from or Cascade.Status.ACTIVE
        c.held_from, c.watch_from = "", timezone.now()
        return [("resumed", "Resumed: sells re-placed and the next level re-checked with a fresh price", None)]
    _mutate(cascade, fn)
    if price:
        step(cascade.pk, price)


def cancel(cascade: Cascade) -> None:
    """Stop for good: withdraw resting orders; levels not yet bought are cancelled. Bought coins are kept."""
    def fn(c):
        if c.status in (Cascade.Status.COMPLETED, Cascade.Status.CANCELLED):
            raise ValueError("This cascade has already finished")
        c.status = Cascade.Status.CANCELLED
        c.levels.filter(status__in=(L.PLACED, L.PENDING)).update(status=L.CANCELLED, placed_at=None)
        held = c.levels.filter(status=L.FILLED, sell_status=S.PLACED).update(sell_status=S.NONE, sell_placed_at=None)
        return [("cancelled", "Cancelled" + (f": {held} bought level(s) kept without a sell" if held else ""), None)]
    _mutate(cascade, fn)
    _reconcile_if_live(cascade)


def cancel_level(level: CascadeLevel, side: str) -> None:
    """Withdraw one level's resting order. A buy goes back to pending (re-checked on the next step); a sell
    is re-placed fresh at its computed take-profit on the next step."""
    def fn(c):
        lv = c.levels.get(pk=level.pk)
        if side == "buy":
            if lv.status != L.PLACED:
                raise ValueError("That level has no resting buy")
            lv.status, lv.placed_at = L.PENDING, None
        elif side == "sell":
            if lv.sell_status != S.PLACED:
                raise ValueError("That level has no resting sell")
            lv.sell_status, lv.sell_placed_at, lv.sell_target = S.NONE, None, None
        else:
            raise ValueError("side must be buy or sell")
        lv.save(update_fields=ENGINE_FIELDS)
        return [("level_cancelled", f"{side.title()} order withdrawn", lv.index)]
    _mutate(level.cascade, fn)
    _reconcile_if_live(level.cascade)


def edit_level(level: CascadeLevel, *, target_price: float | None = None, amount_usdt: float | None = None,
               take_profit_pct: float | None = None, sell_target: float | None = None) -> None:
    """Pending level: change buy price / amount / take-profit. Resting buy: re-placed at the new price /
    amount. Resting sell: re-placed at the new sell price."""
    def fn(c):
        lv = c.levels.get(pk=level.pk)
        now = timezone.now()
        if lv.status in (L.PENDING, L.PLACED):
            for name, v in (("buy price", target_price), ("amount", amount_usdt)):
                if v is not None and not v > 0:
                    raise ValueError(f"The {name} must be above 0")
            if take_profit_pct is not None and not take_profit_pct > 0:
                raise ValueError("The take-profit must be above 0 %")
            lv.target_price = target_price or lv.target_price
            lv.amount_usdt = amount_usdt or lv.amount_usdt
            lv.take_profit_pct = take_profit_pct or lv.take_profit_pct
            if lv.status == L.PLACED:
                lv.placed_at = now  # a replaced order only fills on prices from now on
            msg = f"Edited: buy {fmt(lv.amount_usdt)} at {fmt(lv.target_price)}, take-profit {lv.take_profit_pct:.2f}%"
        elif lv.status == L.FILLED and lv.sell_status == S.PLACED:
            if not (sell_target and sell_target > 0):
                raise ValueError("The sell price must be above 0")
            lv.sell_target, lv.sell_placed_at = sell_target, now
            msg = f"Edited: take-profit sell moved to {fmt(sell_target)}"
        else:
            raise ValueError("Only a pending level, a resting buy or a resting sell can be edited")
        lv.save(update_fields=ENGINE_FIELDS)
        return [("edited", msg, lv.index)]
    _mutate(level.cascade, fn)
    _reconcile_if_live(level.cascade)
