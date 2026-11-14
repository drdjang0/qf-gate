"""Backtests: replay stored candles through the engine and record trades + metrics."""

from __future__ import annotations

import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from market.services.funding import funding_map

from .data import load_bars
from .engine import Bar, Engine, compute_metrics
from .models import BacktestRun, Trade
from .strategies.base import get_strategy

WARMUP_DAYS = 2  # bars before `start` that only build day levels / indicators (no trading)
MAX_EQUITY_POINTS = 1500


def warmup(strategy_key: str, params: dict) -> timedelta:
    """History to replay before `start`: the larger of the default and what the strategy asks for."""
    return timedelta(days=max(WARMUP_DAYS, get_strategy(strategy_key)(params).warmup_days()))


def run_backtest(run: BacktestRun) -> BacktestRun:
    BacktestRun.objects.filter(pk=run.pk).update(status=BacktestRun.Status.RUNNING, message="")
    try:
        return _run(run)
    except Exception as exc:
        BacktestRun.objects.filter(pk=run.pk).update(
            status=BacktestRun.Status.FAILED, message=str(exc)[:4000], finished_at=timezone.now()
        )
        raise


MAX_SIGNALS = 5000
MAX_PLOT_POINTS = 3000


def _recorder(engine: Engine, record: dict):
    """Wrap the strategy so every signal (and its plot values) is recorded during the run."""
    strat = engine.strategy
    original = strat.on_bar

    def watched(ctx):
        sig = original(ctx)
        record["_last"] = (ctx.bar.time, sig)
        values = strat.plot(ctx)
        if values:
            record["_plot_now"] = (ctx.bar.time, values)
        return sig

    strat.on_bar = watched


def _after_bar(engine: Engine, record: dict, trading: bool) -> None:
    t, sig = record.pop("_last", (None, None))
    pt = record.pop("_plot_now", None)
    if not trading:
        return
    if pt:
        for name, value in pt[1].items():
            record["plot"].setdefault(name, []).append([pt[0], value])
    if sig is None or len(record["signals"]) >= MAX_SIGNALS:
        return
    pending = engine.pending
    if pending and pending.get("reason") == sig.reason and pending.get("action") == sig.action:
        status = "queued"
    elif sig.action == "buy" and engine.pos:
        status = "ignored: already in a position"
    elif sig.action == "sell" and not engine.pos:
        status = "ignored: no position"
    elif sig.action == "buy" and engine.pending:
        status = "ignored: an order is already waiting"
    elif engine.index - 1 <= engine.lock_until:
        status = f"blocked: {engine.lock_reason}"
    else:
        status = "blocked: order-flow filter or no stop"
    record["signals"].append({"time": t, "action": sig.action, "reason": sig.reason, "stop": sig.stop,
                              "target": sig.target, "tag": sig.tag, "status": status})


def simulate(bars: list[Bar], strategy_key: str, params: dict, risk: dict, start_ts: int,
             end_ts: int | None = None, record: dict | None = None, funding: dict | None = None
             ) -> tuple[list[dict], list[tuple[int, float]], dict] | None:
    """Run one strategy over `bars`; trade only on bars with start_ts <= time (< end_ts).

    Bars before start_ts are warm-up (day levels, strategy state). An open position is
    closed at the last traded bar's close. Returns (trades, equity, metrics), or None
    when there is no bar to trade in the window.
    """
    engine = Engine(get_strategy(strategy_key)(params), risk, settings.MARKET["DAY_TIMEZONE"], funding=funding)
    if record is not None:
        record.setdefault("signals", [])
        record.setdefault("plot", {})
        _recorder(engine, record)
    trades: list[dict] = []
    equity: list[tuple[int, float]] = []
    in_position = total = 0
    first_open = last = None
    for bar in bars:
        if end_ts is not None and bar.time >= end_ts:
            break
        trading = bar.time >= start_ts
        trades += engine.process(bar, trading=trading)
        if record is not None:
            _after_bar(engine, record, trading)
        if trading:
            total += 1
            first_open = first_open if first_open is not None else bar.open
            in_position += engine.pos is not None
            equity.append((bar.end, engine.equity(bar.close)))
            last = bar
    if total == 0:
        return None
    final_trade = engine.close_all(last.end, last.close)
    if final_trade:
        trades.append(final_trade)
        equity[-1] = (last.end, engine.equity(last.close))
    metrics = compute_metrics(
        trades, equity, engine.risk["starting_cash"], in_position, total,
        buy_hold_pct=(last.close / first_open - 1) * 100,
    )
    metrics["skipped_entries"] = engine.skipped
    metrics["expired_limits"] = engine.expired  # limit entries that never filled
    metrics["intrabar_resolved"] = engine.detail_used  # bars where the 1m path decided stop vs target
    metrics["blocked_entries"] = dict(engine.blocked)
    metrics["funding"] = engine.funding_paid  # futures: funding paid (+) / received (-)
    metrics["liquidations"] = engine.liquidations  # entry signals blocked by protections, per reason
    metrics["bars"] = total
    return trades, equity, metrics


def _run(run: BacktestRun) -> BacktestRun:
    coin = run.config.coin
    bars = load_bars(coin, run.interval, run.start - warmup(run.strategy_key, run.params), run.end)
    record: dict = {}
    funding = funding_map(coin, run.start - warmup(run.strategy_key, run.params), run.end)
    result = simulate(bars, run.strategy_key, run.params, run.risk, int(run.start.timestamp()), record=record,
                      funding=funding)
    if result is None:
        raise ValueError(f"No closed {run.interval} candles for {coin.pair} in this period. Sync the coin first.")
    trades, equity, metrics = result
    total = metrics.pop("bars")
    every = max(1, math.ceil(len(equity) / MAX_EQUITY_POINTS))
    sampled = equity[::every]
    if sampled[-1] != equity[-1]:
        sampled.append(equity[-1])

    with transaction.atomic():
        run.trades.all().delete()
        Trade.objects.bulk_create([Trade.from_engine(t, backtest=run) for t in trades])
        run.metrics = metrics
        run.equity = [[t, round(e, 6)] for t, e in sampled]
        run.bars = total
        every_p = max(1, math.ceil(max((len(v) for v in record["plot"].values()), default=0) / MAX_PLOT_POINTS))
        run.plot = {k: v[::every_p] for k, v in record["plot"].items()}
        run.signals = record["signals"]
        run.status = BacktestRun.Status.DONE
        run.message = ""
        run.finished_at = timezone.now()
        run.save()
    return run


def handle_job(job) -> str:
    """Worker handler for market Job(kind='backtest', params={'run_id': ...})."""
    run = BacktestRun.objects.select_related("config__coin").get(pk=job.params["run_id"])
    run = run_backtest(run)
    m = run.metrics
    return f"Backtest #{run.pk}: {m['trades']} trades, return {m['return_pct']:.2f}%, max DD {m['max_drawdown_pct']:.2f}%"
