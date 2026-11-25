"""Bots: a strategy trading through a broker (dry-run now, live later).

How a bot works
- Decisions: the same Engine as backtests and paper trading runs on every closed bar (state saved
  between steps). The engine's own simulated trades are kept as the bot's *model* trades.
- Orders: the moment the engine decides (a pending buy/sell after a closed bar, or the engine
  closing its position), the bot sends a market order through its broker. It does not wait for the
  next bar, unlike the simulation.
- Between bars: every live price tick from the stream is checked against the stop and target
  (including trailing / break-even moves the engine made), so exits are not a bar late.
- Safety: entries go through the risk limits and the kill switch; exits are never blocked.
  Every order is recorded (BotOrder) with the decision price and the actual fill.
- Concurrency: the stream and the worker may act on the same bot; a version counter (compare-and-
  set) makes sure only one of them sends a given order.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone as dj_tz

from market.exchanges.base import ExchangeError
from market.exchanges.gate import get_client
from market.models import Coin
from market.services.chart import CHART_INTERVALS

from . import notify, signals_share
from .backtest import warmup
from .brokers import GateBroker, OrderError, client_id, get_broker, live_allowed
from .data import load_bars
from .engine import Engine, size_for
from .models import Bot, BotOrder, StrategyConfig, Trade, TradingControl
from .strategies.base import RISK_PARAMS, clean_params, get_strategy

log = logging.getLogger("trading.bots")


# -- risk limits and the kill switch --------------------------------------------------------------


ALL = object()  # owner filter sentinel: every user's bots (operator tools, legacy single-user calls)


def _mine(qs, owner, path: str = "config__owner"):
    """Limit to one user's rows (multi-user). ALL keeps everything; owner None = rows without an owner."""
    if owner is ALL:
        return qs
    return qs.filter(**{path: owner}) if owner is not None else qs.filter(**{f"{path}__isnull": True})


def _owner_of(bot: Bot):
    return bot.config.owner if bot.config.owner_id else None


def _capital(mode: str, owner=ALL) -> float:
    bots = _mine(Bot.objects.filter(status=Bot.Status.RUNNING, mode=mode), owner)
    return sum(clean_params(RISK_PARAMS, b.risk)["starting_cash"] for b in bots) or 1.0


def realised_today(mode: str, owner=ALL) -> float:
    start = dj_tz.now().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    qs = BotOrder.objects.filter(bot__mode=mode, side="sell", status=BotOrder.Status.FILLED, created_at__gte=start)
    return _mine(qs, owner, "bot__config__owner").aggregate(s=Sum("pnl"))["s"] or 0.0


def exposure(mode: str, owner=ALL) -> float:
    bots = _mine(Bot.objects.filter(status=Bot.Status.RUNNING, mode=mode), owner)
    return sum(b.pos["qty"] * b.pos["avg"] for b in bots if b.pos)


def entry_block_reason(bot: Bot, notional: float) -> str | None:
    """Why a new entry may not be sent right now (None = allowed). Exits are never checked.
    Every limit is the bot owner's own (multi-user); a limit left empty / 0 is off."""
    owner = _owner_of(bot)
    c = TradingControl.for_user(owner)
    if c.halted:
        return f"kill switch on ({c.halted_reason or 'halted'})"
    if bot.mode == Bot.Mode.LIVE and not c.live_enabled:
        return "live trading is switched off in your settings"
    hour_ago = dj_tz.now() - timedelta(hours=1)
    recent = _mine(BotOrder.objects.filter(created_at__gte=hour_ago, status=BotOrder.Status.FILLED), owner,
                   "bot__config__owner")
    if c.max_orders_per_hour and recent.count() >= c.max_orders_per_hour:
        return f"more than {c.max_orders_per_hour} orders in the last hour"
    loss = -realised_today(bot.mode, owner)
    if c.max_daily_loss_pct and loss >= _capital(bot.mode, owner) * c.max_daily_loss_pct / 100:
        halt(f"daily loss limit {c.max_daily_loss_pct}% reached ({loss:,.2f})", by="risk limit", owner=owner)
        return "daily loss limit reached"
    if c.max_exposure and exposure(bot.mode, owner) + notional > c.max_exposure:
        return f"max exposure {c.max_exposure:,.2f} would be exceeded"
    return None


def halt(reason: str, by: str = "user", owner=None) -> None:
    c = TradingControl.for_user(owner)
    if not c.halted:
        TradingControl.objects.filter(pk=c.pk).update(halted=True, halted_reason=reason[:255], halted_at=dj_tz.now())
        who = f" [{owner.username}]" if owner is not None else ""
        notify.notify("control.halt", f"{reason} (by {by}){who}. No new entries; open positions are still managed.")


def resume(by: str = "user", owner=None) -> None:
    c = TradingControl.for_user(owner)
    TradingControl.objects.filter(pk=c.pk).update(halted=False, halted_reason="", halted_at=None)
    notify.notify("control.resume", f"Entries allowed again (by {by}).")


def kill(by: str = "user", broker=None, owner=ALL) -> int:
    """Kill switch: halt entries, sell every open bot position at market, stop all bots (of one user, or all)."""
    if owner is ALL:
        halt("kill switch: flatten and stop", by=by)
        for c in TradingControl.objects.exclude(owner=None):
            halt("kill switch: flatten and stop", by=by, owner=c.owner)
    else:
        halt("kill switch: flatten and stop", by=by, owner=owner)
    sold = 0
    for b in _mine(Bot.objects.filter(status=Bot.Status.RUNNING), owner):
        if b.pos and execute(b.pk, "sell", "kill switch", broker=broker):
            sold += 1
        stop_bot(b, note=f"kill switch ({by})")
    return sold


# -- lifecycle ------------------------------------------------------------------------------------


def promotion_status(config: StrategyConfig) -> dict:
    """Checklist a config must pass before a live bot may start (dry-run -> live promotion)."""
    owner = config.owner if config.owner_id else None
    c = TradingControl.for_user(owner)
    ok_live, missing = live_allowed(owner)
    dry = config.bots.filter(mode=Bot.Mode.DRY_RUN).order_by("started_at").first()
    days = ((dry.stopped_at or dj_tz.now()) - dry.started_at).days if dry else 0
    check = config.last_check or {}
    items = []
    if c.require_checks:
        items += [
            {"ok": days >= c.min_dry_run_days, "text": f"Dry-run for at least {c.min_dry_run_days} days (has {days})"},
            {"ok": bool(check.get("lookahead", {}).get("ok")), "text": "Strategy checks: no look-ahead"},
            {"ok": bool(check) and check.get("warmup", {}).get("ok") is not False,
             "text": "Strategy checks: warm-up long enough"},
        ]
    else:
        items.append({"ok": True, "text": "Strategy checks switched off in your settings (your own risk)"})
    items += [{"ok": False, "text": m} for m in missing]
    if ok_live:
        items.append({"ok": True, "text": "Live trading switches are on"})
    return {"ok": all(i["ok"] for i in items), "items": items}


def start_bot(config: StrategyConfig, mode: str = Bot.Mode.DRY_RUN) -> Bot:
    get_strategy(config.strategy_key)(config.params)  # validate
    if config.owner_id and not TradingControl.for_user(config.owner).strategies_enabled:
        raise ValueError("Strategies are switched off in your settings (Account → Trading settings).")
    if clean_params(RISK_PARAMS, config.risk)["market"] == "futures" or config.strategy_key.endswith("_short"):
        raise ValueError("Bots trade Gate.io spot only: futures / short strategies run in backtests, sweeps and "
                         "paper trading for now.")
    if mode == Bot.Mode.LIVE:
        status = promotion_status(config)
        if not status["ok"]:
            raise ValueError("Not ready for live: " + "; ".join(i["text"] for i in status["items"] if not i["ok"]))
    risk = clean_params(RISK_PARAMS, config.risk)
    with transaction.atomic():
        bot = Bot.objects.create(config=config, mode=mode, strategy_key=config.strategy_key, interval=config.interval,
                                 params=config.params, risk=config.risk,
                                 state={"engine": {}, "pos": None, "cash": risk["starting_cash"], "busy": False})
    notify.notify("bot.start", f"#{bot.pk} {config.name} ({bot.get_mode_display()}) on {config.coin.pair} {config.interval}")
    return bot


def stop_bot(bot: Bot, note: str = "") -> None:
    Bot.objects.filter(pk=bot.pk, status=Bot.Status.RUNNING).update(status=Bot.Status.STOPPED, stopped_at=dj_tz.now())
    still_open = Bot.objects.get(pk=bot.pk).pos
    notify.notify("bot.stop", f"#{bot.pk} {bot.config.name}" + (f": {note}" if note else "")
                  + (" (position still open; use Force exit or manage it on Gate.io)" if still_open else ""))


# -- orders ---------------------------------------------------------------------------------------


def _last_price(bot: Bot) -> float:
    t = get_client().get_ticker(bot.config.coin.pair) or {}
    p = float(t.get("last") or 0)
    if p <= 0:
        raise OrderError("No last price from Gate.io")
    return p


def execute(bot_id: int, side: str, reason: str, ref_price: float | None = None,
            stop: float | None = None, target: float | None = None, broker=None) -> BotOrder | None:
    """Send one order for a bot, at most once even if several processes try at the same moment."""
    bot = Bot.objects.select_related("config__coin").get(pk=bot_id)
    st = dict(bot.state or {})
    if side == "buy" and bot.status != Bot.Status.RUNNING:
        return None
    if st.get("busy") or (side == "buy" and st.get("pos")) or (side == "sell" and not st.get("pos")):
        return None
    try:
        price = ref_price or _last_price(bot)
    except (OrderError, ExchangeError) as exc:
        _error(bot, f"{side} skipped: {exc}")
        return None
    risk = clean_params(RISK_PARAMS, bot.risk)
    if side == "buy":
        if stop is None or stop >= price:
            return None  # price already through the stop: same rule as the engine
        qty = size_for(risk, price, stop, st["cash"], st["cash"], risk["fee_rate"])
        why = entry_block_reason(bot, qty * price)
        if why or qty <= 0:
            o = BotOrder.objects.create(bot=bot, side=side, reason=reason, qty=qty, ref_price=price,
                                        status=BotOrder.Status.BLOCKED, message=why or "size is zero")
            notify.notify("bot.blocked", f"#{bot.pk} {bot.config.coin.pair} buy blocked: {o.message}")
            return o
    else:
        qty = st["pos"]["qty"]
    # claim the order slot (compare-and-set) before talking to the broker; remember our order id first,
    # so an order interrupted by a crash can be found on Gate.io afterwards (see recover_stuck)
    cid = client_id(f"b{bot.pk}x")
    busy = {**st, "busy": True, "busy_since": time.time(), "client_id": cid, "busy_side": side, "busy_reason": reason,
            "busy_ref": price, "busy_stop": stop, "busy_target": target}
    if not Bot.objects.filter(pk=bot.pk, version=bot.version).update(version=bot.version + 1, state=busy):
        return None
    version = bot.version + 1
    try:
        b = broker or get_broker(bot)
        if side == "sell" and (st.get("pos") or {}).get("stop_order"):
            done = _settle_exchange_stop(bot, b, version, st, price)
            if done is not None:
                return done  # the stop on Gate.io had already sold the position
        fill = b.market(bot.config.coin.pair, side, qty, **({"text": cid} if isinstance(b, GateBroker) else {}))
    except (OrderError, ExchangeError) as exc:
        Bot.objects.filter(pk=bot.pk, version=version).update(version=version + 1, state={**st, "busy": False},
                                                              last_error=str(exc)[:2000])
        o = BotOrder.objects.create(bot=bot, side=side, reason=reason, qty=qty, ref_price=price,
                                    status=BotOrder.Status.ERROR, message=str(exc)[:2000])
        notify.notify("bot.error", f"#{bot.pk} {side} {qty:g} {bot.config.coin.pair} failed: {exc}")
        return o
    o = _book_fill(bot, version, st, side, reason, qty, price, fill, stop, target)
    if side == "buy" and isinstance(b, GateBroker) and stop and bot.risk.get("exchange_stop", True):
        _place_exchange_stop(bot, b, fill.qty, stop)
    return o


def _book_fill(bot: Bot, version: int, st: dict, side: str, reason: str, qty: float, price: float, fill,
               stop=None, target=None) -> BotOrder:
    """Apply a fill to the bot's state and record the order (shared by normal orders, a stop that fired on
    Gate.io and an order recovered after a crash)."""
    st = {k: v for k, v in st.items() if not k.startswith("busy_") and k != "client_id"}
    pnl = None
    if side == "buy":
        st["cash"] -= fill.qty * fill.avg_price + fill.fee
        st["pos"] = {"qty": fill.qty, "avg": fill.avg_price, "stop": stop, "target": target, "fee": fill.fee,
                     "entry_time": int(datetime.now(timezone.utc).timestamp()), "reason": reason}
    else:
        pos = st["pos"]
        proceeds = fill.qty * fill.avg_price - fill.fee
        pnl = proceeds - (pos["qty"] * pos["avg"] + pos["fee"])
        st["cash"] += proceeds
        st["pos"] = None
    st["busy"] = False
    Bot.objects.filter(pk=bot.pk).update(version=version + 1, state=st, last_error="")
    o = BotOrder.objects.create(bot=bot, side=side, reason=reason, qty=qty, ref_price=price,
                                status=BotOrder.Status.FILLED, filled_qty=fill.qty, avg_price=fill.avg_price,
                                fee=fill.fee, pnl=pnl, exchange_id=fill.exchange_id)
    tail = (f", P&L {pnl:+,.2f}" if pnl is not None
            else (f", stop {stop:,.6g}" if stop else "") + (f", target {target:,.6g}" if target else ""))
    mode = "DRY-RUN" if bot.mode == Bot.Mode.DRY_RUN else "LIVE"
    notify.notify("bot.fill", f"#{bot.pk} {mode} {side.upper()} {fill.qty:.6g} {bot.config.coin.pair} "
                              f"@ {fill.avg_price:,.6g} ({reason}){tail}",
                  {"bot": bot.pk, "side": side, "qty": fill.qty, "price": fill.avg_price, "pnl": pnl})
    return o


# -- live safety: the stop on Gate.io, and orders interrupted by a crash ----------------------------------


def _place_exchange_stop(bot: Bot, broker, qty: float, stop: float) -> None:
    """Also put the stop-loss on Gate.io as a price-triggered sell, so it holds while QF-gate is down."""
    try:
        sid = broker.place_stop(bot.config.coin.pair, qty, stop)
    except (OrderError, ExchangeError) as exc:
        _error(bot, f"stop on Gate.io not placed: {exc}")
        notify.notify("bot.error", f"#{bot.pk} LIVE: the stop on Gate.io could not be placed ({exc}); QF-gate "
                                   f"still watches the stop itself.")
        return
    fresh = Bot.objects.get(pk=bot.pk)
    if fresh.pos:
        _remember(bot.pk, pos={**fresh.pos, "stop_order": sid, "stop_on_exchange": stop})


def _move_exchange_stop(bot: Bot, pos: dict, new_stop: float, broker=None) -> dict:
    """The engine moved the stop (trailing / break-even): move the order on Gate.io too."""
    if not pos.get("stop_order") or pos.get("stop_on_exchange") == new_stop or bot.mode != Bot.Mode.LIVE:
        return pos
    try:
        b = broker or get_broker(bot)
        b.cancel_stop(pos["stop_order"])
        sid = b.place_stop(bot.config.coin.pair, pos["qty"], new_stop)
        return {**pos, "stop_order": sid, "stop_on_exchange": new_stop}
    except (OrderError, ExchangeError) as exc:
        _error(bot, f"moving the stop on Gate.io failed: {exc}")
        return pos


def _settle_exchange_stop(bot: Bot, broker, version: int, st: dict, price: float) -> BotOrder | None:
    """Before selling: cancel our stop on Gate.io; if it already fired, book that sale instead."""
    pos = st["pos"]
    info = broker.stop_status(pos["stop_order"])
    if info.get("status") == "finish" and info.get("fired_order_id"):
        order = broker.get_order(bot.config.coin.pair, str(info["fired_order_id"]))
        fill = broker.fill_from(order, bot.config.coin.pair)
        if fill is not None:
            return _book_fill(bot, version, st, "sell", "stop (on Gate.io)", pos["qty"], price, fill)
    if info.get("status") == "open":
        broker.cancel_stop(pos["stop_order"])
    return None


def check_exchange_stop(bot: Bot, broker=None) -> BotOrder | None:
    """Periodic (live bots with a stop on Gate.io): did it fire while we were not looking?"""
    st = dict(bot.state or {})
    pos = st.get("pos") or {}
    if bot.mode != Bot.Mode.LIVE or not pos.get("stop_order") or st.get("busy"):
        return None
    b = broker or get_broker(bot)
    info = b.stop_status(pos["stop_order"])
    if info.get("status") != "finish" or not info.get("fired_order_id"):
        return None
    if not Bot.objects.filter(pk=bot.pk, version=bot.version).update(version=bot.version + 1, state={**st, "busy": True}):
        return None
    order = b.get_order(bot.config.coin.pair, str(info["fired_order_id"]))
    fill = b.fill_from(order, bot.config.coin.pair)
    if fill is None:
        Bot.objects.filter(pk=bot.pk).update(state={**st, "busy": False})
        return None
    return _book_fill(bot, bot.version + 1, st, "sell", "stop (on Gate.io)", pos["qty"], fill.avg_price, fill)


RECOVER_AFTER_SECONDS = 120


def recover_stuck(bot: Bot, broker=None) -> BotOrder | None:
    """An order slot left busy (the process died between sending and saving): look the order up on Gate.io
    by our client id and book it if it filled; otherwise free the slot. Dry-run fills are instant and send
    nothing, so a stuck dry-run slot is simply freed."""
    st = dict(bot.state or {})
    if not st.get("busy") or time.time() - float(st.get("busy_since") or 0) < RECOVER_AFTER_SECONDS:
        return None
    side = st.get("busy_side")
    found = None
    if bot.mode == Bot.Mode.LIVE and st.get("client_id") and side:
        try:
            b = broker or get_broker(bot)
            order = b.find_order(bot.config.coin.pair, st["client_id"], float(st["busy_since"]))
            found = b.fill_from(order, bot.config.coin.pair) if order else None
        except (OrderError, ExchangeError) as exc:
            _error(bot, f"recovering an interrupted order failed: {exc}")
            return None
    clean = {k: v for k, v in st.items() if not k.startswith("busy_") and k != "client_id"}
    if found is None:
        Bot.objects.filter(pk=bot.pk, version=bot.version).update(version=bot.version + 1, state={**clean, "busy": False})
        notify.notify("bot.error", f"#{bot.pk}: an interrupted {side or ''} order was not filled; the bot carries on.")
        return None
    o = _book_fill(bot, bot.version, clean, side, (st.get("busy_reason") or "") + " (recovered)",
                   found.qty, float(st.get("busy_ref") or found.avg_price), found, st.get("busy_stop"), st.get("busy_target"))
    notify.notify("bot.fill", f"#{bot.pk}: recovered an interrupted {side} order from Gate.io.")
    return o


def _error(bot: Bot, msg: str) -> None:
    Bot.objects.filter(pk=bot.pk).update(last_error=msg[:2000])
    log.warning("Bot %s: %s", bot.pk, msg)


def force_exit(bot: Bot, by: str = "user", broker=None) -> BotOrder | None:
    return execute(bot.pk, "sell", f"force exit ({by})", broker=broker)


# -- the decision loop -------------------------------------------------------------------------------


def _remember(bot_id: int, **changes) -> None:
    fresh = Bot.objects.get(pk=bot_id)
    Bot.objects.filter(pk=bot_id, version=fresh.version).update(version=fresh.version + 1,
                                                               state={**fresh.state, **changes})


def step_bot(bot_id: int, now: datetime | None = None, broker=None) -> int:
    """Feed newly closed bars to the bot's engine, then act on its decisions. Returns orders sent."""
    bot = Bot.objects.select_related("config__coin").get(pk=bot_id)
    if bot.status != Bot.Status.RUNNING:
        return 0
    if (bot.state or {}).get("busy"):
        recover_stuck(bot, broker if bot.mode == Bot.Mode.LIVE else None)
        bot.refresh_from_db()
    elif bot.mode == Bot.Mode.LIVE and (bot.pos or {}).get("stop_order"):
        try:
            check_exchange_stop(bot, broker)
        except (OrderError, ExchangeError) as exc:
            _error(bot, f"checking the stop on Gate.io failed: {exc}")
        bot.refresh_from_db()
    strategy = get_strategy(bot.strategy_key)(bot.params)
    now = now or datetime.now(timezone.utc)
    step = CHART_INTERVALS[bot.interval]
    if bot.last_bar_time is None:
        from_dt = bot.started_at - warmup(bot.strategy_key, bot.params)
    else:
        from_dt = datetime.fromtimestamp(bot.last_bar_time + step, tz=timezone.utc)
    bars = load_bars(bot.config.coin, bot.interval, from_dt, now + timedelta(minutes=1))
    if not bars:
        return 0
    st = dict(bot.state or {})
    engine = Engine(strategy, bot.risk, settings.MARKET["DAY_TIMEZONE"], state=st.get("engine") or None)
    from intel.gate import make_gate  # 7.7 AI filter: dry-run only, no-op unless switched on and earned

    engine.entry_gate = make_gate(bot.config.coin, bot.risk, bot.mode)
    start_ts = int(bot.started_at.timestamp())
    closed = []
    for bar in bars:
        closed += engine.process(bar, trading=bar.time >= start_ts)
    prev_pending = (st.get("engine") or {}).get("pending")
    st["engine"] = engine.state()
    if not Bot.objects.filter(pk=bot.pk, version=bot.version, last_bar_time=bot.last_bar_time).update(
            version=bot.version + 1, state=st, last_bar_time=engine.last_time):
        return 0  # another process stepped (or acted on) this bot first
    Trade.objects.bulk_create([Trade.from_engine(t, bot_id=bot.pk) for t in closed])
    if engine.pending and engine.pending != prev_pending:
        signals_share.record(bot.config.name, bot.config.coin.pair, bot.interval, engine.pending, engine.last_time)

    # act on the engine's decisions now, not at the next bar
    sent = 0
    pos, pending = st.get("pos"), engine.pending
    if pos:
        if pending and pending.get("action") == "sell":
            sent += bool(execute(bot.pk, "sell", pending.get("reason") or "signal", broker=broker))
        elif engine.pos is None and closed:
            sent += bool(execute(bot.pk, "sell", closed[-1]["exit_reason"], broker=broker))
        elif engine.pos and not Bot.objects.get(pk=bot.pk).state.get("busy"):
            moved = {**pos, "stop": engine.pos["stop"], "target": engine.pos["target"]}
            if engine.pos["stop"]:
                moved = _move_exchange_stop(bot, moved, engine.pos["stop"], broker)
            _remember(bot.pk, pos=moved)
    elif pending and pending.get("action") == "buy" and engine.pos is None and st.get("acted") != engine.last_time:
        sent += bool(execute(bot.pk, "buy", pending.get("reason") or "signal", stop=pending.get("stop"),
                             target=pending.get("target"), broker=broker))
        _remember(bot.pk, acted=engine.last_time)  # act once per decision, even if it was blocked
    return sent


def step_all(coin: Coin | None = None) -> int:
    qs = Bot.objects.filter(status=Bot.Status.RUNNING)
    if coin is not None:
        qs = qs.filter(config__coin=coin)
    n = 0
    for b in qs.values_list("pk", flat=True):
        try:
            n += step_bot(b)
        except Exception as exc:  # noqa: BLE001 - one bot must not stop the others
            log.exception("Bot %s step failed", b)
            Bot.objects.filter(pk=b).update(status=Bot.Status.ERROR, last_error=str(exc)[:2000])
            notify.notify("bot.error", f"#{b} stopped by an error: {exc}")
    return n


def on_price(coin: Coin, price: float, broker=None) -> int:
    """Live tick from the stream: exit at once if price reached a bot's stop or target."""
    sent = 0
    for b in Bot.objects.filter(status=Bot.Status.RUNNING, config__coin=coin):
        pos = b.pos
        if not pos or (b.state or {}).get("busy"):
            continue
        if pos.get("stop") is not None and price <= pos["stop"]:
            sent += bool(execute(b.pk, "sell", "stop (live price)", ref_price=price, broker=broker))
        elif pos.get("target") is not None and price >= pos["target"]:
            sent += bool(execute(b.pk, "sell", "target (live price)", ref_price=price, broker=broker))
    return sent


def check_services() -> None:
    """Worker periodic: alert (at most every 30 min) when bots run but the live stream is down."""
    from market.services.jobs import service_status

    if not Bot.objects.filter(status=Bot.Status.RUNNING).exists():
        return
    stream = service_status().get("stream")
    stale = settings.TRADING["STALE_SERVICE_SECONDS"]
    if stream and (dj_tz.now() - stream.last_seen).total_seconds() < stale:
        return
    if cache.add("alert:stream-down", 1, 1800):
        notify.notify("service.down", "The live stream is not running: bots only act once a minute (worker) "
                                      "and stops are checked on closed bars only. Start stream_candles.")
