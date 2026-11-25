"""Live cascades: keep the real limit orders on Gate.io in step with what the cascade engine decided.

The engine (engine.py) decides *what* should rest: which level's buy is placed, which bought level has a
take-profit sell, at what price. This module makes Gate.io match that, and reports back what Gate.io did:

- a level marked "Buy resting" with no order yet  -> send a limit buy (our client id is saved first, so if
  the process dies between sending and saving, the order is found again by that id);
- an order whose level is no longer resting (hold, cancel, withdraw) or whose price / amount was edited
  -> cancel it on Gate.io (an edited one is sent again at the new price on the next pass);
- an order Gate.io reports as filled -> the level is bought / sold with Gate.io's exact amount, price and
  fee (a buy's fee taken in the coin is subtracted, so the take-profit sell never asks for more than you hold);
- an order cancelled on Gate.io with nothing filled -> the level goes back to pending and is re-checked.

It runs after every engine step (worker every 30 s, live price ticks) and at once when the fill listener
(stream_account) hears from Gate.io that one of our orders changed, so a fill is acted on within moments.
Fills are written under the same compare-and-set version as the engine, so two processes never book one twice.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from market.exchanges.base import ExchangeError

from .models import Cascade, CascadeEvent, CascadeLevel

log = logging.getLogger(__name__)
L, S = CascadeLevel.Status, CascadeLevel.SellStatus
ID_FIELDS = ["buy_client_id", "buy_order_id", "buy_order_price", "sell_client_id", "sell_order_id",
             "sell_order_price", "order_error"]


def broker_for(c: Cascade):
    from trading.brokers import GateBroker, OrderError, _credentials

    creds = _credentials(c.owner if c.owner_id else None)
    if not creds:
        raise OrderError("No Gate.io API key for this cascade's owner")
    return GateBroker(*creds)


def _fmt(x):
    from .engine import fmt

    return fmt(x)


def _book(c_pk: int, fn) -> bool:
    """Apply a fill under the cascade's compare-and-set version (the engine uses the same one)."""
    with transaction.atomic():
        c = Cascade.objects.get(pk=c_pk)
        notes = fn(c) or []
        if not Cascade.objects.filter(pk=c.pk, version=c.version).update(version=c.version + 1, updated_at=timezone.now()):
            return False  # changed meanwhile: the order is still filled on Gate.io, the next pass books it
        CascadeEvent.objects.bulk_create([CascadeEvent(cascade=c, at=timezone.now(), level=lvl, kind=k, price=p,
                                                       message=m[:255]) for k, m, lvl, p in notes])
    return True


def _bought(c_pk: int, lv_pk: int, fill, partial: bool) -> bool:
    def fn(c):
        lv = CascadeLevel.objects.get(pk=lv_pk)
        if lv.status == L.FILLED:
            return []
        lv.status, lv.fill_price, lv.qty, lv.buy_fee, lv.filled_at = L.FILLED, fill.avg_price, fill.qty, fill.fee, timezone.now()
        lv.buy_client_id = lv.buy_order_id = ""
        lv.save()
        return [("bought", f"Bought {_fmt(fill.qty)} at {_fmt(fill.avg_price)} on Gate.io"
                 + (" (partly filled, rest cancelled)" if partial else ""), lv.index, fill.avg_price)]
    return _book(c_pk, fn)


def _sold(c_pk: int, lv_pk: int, fill, partial: bool) -> bool:
    def fn(c):
        lv = CascadeLevel.objects.get(pk=lv_pk)
        if lv.sell_status == S.FILLED:
            return []
        cost = (lv.qty or 0) * (lv.fill_price or 0) + (lv.buy_fee or 0)  # = what the buy really cost
        proceeds = fill.qty * fill.avg_price - fill.fee
        part = fill.qty / lv.qty if lv.qty else 1
        lv.sell_status, lv.sell_filled_at, lv.sell_fee = S.FILLED, timezone.now(), fill.fee
        lv.pnl = proceeds - cost * part
        lv.sell_client_id = lv.sell_order_id = ""
        lv.save()
        return [("sold", f"Sold {_fmt(fill.qty)} at {_fmt(fill.avg_price)} on Gate.io: profit {_fmt(lv.pnl)}"
                 + (" (partly filled, rest cancelled)" if partial else ""), lv.index, fill.avg_price)]
    return _book(c_pk, fn)


def _save_ids(lv: CascadeLevel) -> None:
    lv.save(update_fields=ID_FIELDS)


def _event(c: Cascade, kind: str, msg: str, lv: CascadeLevel | None = None, price=None) -> None:
    CascadeEvent.objects.create(cascade=c, kind=kind, message=msg[:255], level=lv.index if lv else None, price=price)


def _find(b, pair: str, order_id: str, client: str, since) -> dict | None:
    from trading.brokers import OrderError

    if order_id:
        return b.get_order(pair, order_id)
    if client:
        try:
            return b.find_order(pair, client, since.timestamp() if since else timezone.now().timestamp() - 3600)
        except OrderError:
            return None
    return None


def _settle(c: Cascade, b, lv: CascadeLevel, side: str, want: bool, want_price: float | None) -> int:
    """One side of one level: book a fill, cancel what shouldn't rest, clear ids of dead orders."""
    pair = c.coin.pair
    oid, cid = (lv.buy_order_id, lv.buy_client_id) if side == "buy" else (lv.sell_order_id, lv.sell_client_id)
    if not oid and not cid:
        return 0
    since = lv.placed_at if side == "buy" else lv.sell_placed_at
    order = _find(b, pair, oid, cid, since)
    if order is None:  # our id never reached Gate.io: forget it, the order is sent again if still wanted
        setattr(lv, f"{side}_client_id", "")
        _save_ids(lv)
        return 0
    setattr(lv, f"{side}_order_id", str(order.get("id", "")))
    status = order.get("status")
    sent_price = lv.buy_order_price if side == "buy" else lv.sell_order_price
    changed = want_price is not None and sent_price not in (None, want_price)
    if side == "buy" and not changed and lv.target_price and order.get("amount"):
        wanted = lv.amount_usdt / lv.target_price  # the amount was edited: send it again
        changed = abs(float(order["amount"]) - wanted) > max(wanted * 0.01, 1e-12)
    if status == "open" and (not want or changed):
        order = b.cancel_order(pair, lv.buy_order_id if side == "buy" else lv.sell_order_id)
        status = order.get("status") or "cancelled"
        _event(c, "order_cancelled", f"{side.title()} order cancelled on Gate.io"
               + ("" if not want else " (price changed: sent again)"), lv)
    if status == "open":
        _save_ids(lv)
        return 0
    filled = float(order.get("filled_amount") or 0)
    fill = b.fill_from(order, pair) if filled > 0 else None
    partial = status == "cancelled"
    if fill is not None:
        return 1 if (_bought if side == "buy" else _sold)(c.pk, lv.pk, fill, partial) else 0
    # cancelled with nothing filled
    for f in (f"{side}_client_id", f"{side}_order_id"):
        setattr(lv, f, "")
    setattr(lv, f"{side}_order_price", None)
    _save_ids(lv)
    if want and side == "buy":
        CascadeLevel.objects.filter(pk=lv.pk, status=L.PLACED).update(status=L.PENDING, placed_at=None)
        _event(c, "order_gone", "The buy order was cancelled on Gate.io: the level is re-checked", lv)
    elif want:
        CascadeLevel.objects.filter(pk=lv.pk, sell_status=S.PLACED).update(sell_status=S.NONE, sell_placed_at=None)
        _event(c, "order_gone", "The sell order was cancelled on Gate.io: it is placed again", lv)
    return 1


def _send(c: Cascade, b, lv: CascadeLevel, side: str, allowed: bool, why: str) -> int:
    from trading.brokers import OrderError, client_id

    if side == "buy" and not allowed:
        if lv.order_error != why[:255]:
            lv.order_error = why[:255]
            _save_ids(lv)
            _event(c, "blocked", why, lv)
        return 0
    cid = client_id(f"c{lv.pk}{side[0]}")
    setattr(lv, f"{side}_client_id", cid)
    _save_ids(lv)  # saved before sending: an interrupted send is found again by this id
    try:
        if side == "buy":
            order = b.limit(c.coin.pair, "buy", lv.amount_usdt / lv.target_price, lv.target_price, cid)
            lv.buy_order_id, lv.buy_order_price = str(order.get("id", "")), lv.target_price
            msg = f"Limit buy {_fmt(lv.amount_usdt)} {c.coin.quote} at {_fmt(lv.target_price)} sent to Gate.io"
            p = lv.target_price
        else:
            order = b.limit(c.coin.pair, "sell", lv.qty or 0, lv.sell_target, cid)
            lv.sell_order_id, lv.sell_order_price = str(order.get("id", "")), lv.sell_target
            msg = f"Take-profit sell {_fmt(lv.qty)} at {_fmt(lv.sell_target)} sent to Gate.io"
            p = lv.sell_target
    except (OrderError, ExchangeError) as exc:
        setattr(lv, f"{side}_client_id", "")
        err = f"{side} order refused: {exc}"[:255]
        if lv.order_error != err:
            _event(c, "order_error", err, lv)
        lv.order_error = err
        _save_ids(lv)
        Cascade.objects.filter(pk=c.pk).update(last_error=err)
        return 0
    lv.order_error = ""
    _save_ids(lv)
    _event(c, "order_sent", msg, lv, p)
    return 1


def reconcile(cascade_id: int, price: float | None = None, broker=None, _depth: int = 0) -> int:
    """Make Gate.io match the cascade. Returns how many changes it made."""
    from trading.brokers import OrderError, live_allowed

    c = Cascade.objects.select_related("coin", "owner").get(pk=cascade_id)
    if c.mode != Cascade.Mode.LIVE:
        return 0
    try:
        b = broker or broker_for(c)
    except OrderError as exc:
        Cascade.objects.filter(pk=c.pk).update(last_error=str(exc)[:2000])
        return 0
    allowed, missing = live_allowed(c.owner if c.owner_id else None)
    why = "Live trading is not allowed: " + "; ".join(missing) if missing else ""
    running = c.is_running
    n = 0
    for lv in c.levels.order_by("index"):
        try:
            want_buy = running and lv.status == L.PLACED
            got = _settle(c, b, lv, "buy", want_buy, lv.target_price)
            n += got
            lv.refresh_from_db()
            want_sell = c.status in (Cascade.Status.ACTIVE, Cascade.Status.PENDING_TRIGGER) and lv.status == L.FILLED \
                and lv.sell_status == S.PLACED
            n += _settle(c, b, lv, "sell", want_sell, lv.sell_target)
            lv.refresh_from_db()
            if running and lv.status == L.PLACED and not lv.buy_order_id and not lv.buy_client_id:
                n += _send(c, b, lv, "buy", allowed, why)
            if want_sell and lv.sell_status == S.PLACED and not lv.sell_order_id and not lv.sell_client_id and lv.qty:
                n += _send(c, b, lv, "sell", True, "")  # a take-profit only sells what was bought: always allowed
        except (OrderError, ExchangeError) as exc:
            log.warning("Cascade %s level %s: %s", c.pk, lv.index, exc)
            Cascade.objects.filter(pk=c.pk).update(last_error=f"Gate.io: {exc}"[:2000])
    if n and _depth < 2 and price is not None:
        # a fill changes what should rest (its take-profit, the next level): decide it and send it now
        from .engine import Conflict
        from .engine import _step as engine_step

        try:
            with transaction.atomic():
                engine_step(c.pk, price, timezone.now())
        except Conflict:
            pass
        n += reconcile(c.pk, price, b, _depth + 1)
    return n


def reconcile_idle() -> int:
    """Held / cancelled / completed live cascades that may still have orders on Gate.io (worker safety net)."""
    ids = (CascadeLevel.objects.filter(cascade__mode=Cascade.Mode.LIVE)
           .exclude(cascade__status__in=(Cascade.Status.ACTIVE, Cascade.Status.PENDING_TRIGGER))
           .exclude(buy_order_id="", buy_client_id="", sell_order_id="", sell_client_id="")
           .values_list("cascade_id", flat=True).distinct())
    n = 0
    for pk in set(ids):
        try:
            n += reconcile(pk)
        except Exception:  # noqa: BLE001
            log.exception("Cascade %s reconcile failed", pk)
    return n


def on_order_event(order: dict) -> int:
    """The fill listener heard about one of our orders: reconcile its cascade right away."""
    oid = str(order.get("id") or "")
    text = str(order.get("text") or "")
    lv = None
    if oid:
        lv = CascadeLevel.objects.filter(cascade__mode=Cascade.Mode.LIVE).filter(
            models_q(buy_order_id=oid) | models_q(sell_order_id=oid)).select_related("cascade__coin").first()
    if lv is None and text.startswith("t-c"):
        lv = CascadeLevel.objects.filter(cascade__mode=Cascade.Mode.LIVE).filter(
            models_q(buy_client_id=text) | models_q(sell_client_id=text)).select_related("cascade__coin").first()
    if lv is None:
        return 0
    from .engine import live_price

    return reconcile(lv.cascade_id, price=live_price(lv.cascade.coin))


def models_q(**kw):
    from django.db.models import Q

    return Q(**kw)
