"""Portfolio backtests (idea from freqtrade's multi-pair bot): one strategy on several coins with one
shared cash balance and a maximum number of open trades.

Each coin has its own Engine (its own strategy state and day levels); bars from all coins are replayed
in time order. Before each bar the coin's engine is given the shared cash, and an entry is blocked when
the maximum number of positions is already open. Equity = shared cash + every open position at its
coin's last close.
"""

from __future__ import annotations

import math

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from market.services.funding import funding_map

from .backtest import MAX_EQUITY_POINTS, warmup
from .data import load_bars
from .engine import Engine, compute_metrics
from .models import PortfolioRun
from .strategies.base import RISK_PARAMS, clean_params, get_strategy


def simulate_portfolio(bars_by_coin: dict[str, list], strategy_key: str, params: dict, risk: dict,
                       start_ts: int, max_open: int, end_ts: int | None = None,
                       funding_by_coin: dict[str, dict] | None = None) -> dict | None:
    r = clean_params(RISK_PARAMS, risk)
    tz = settings.MARKET["DAY_TIMEZONE"]
    fb = funding_by_coin or {}
    engines = {c: Engine(get_strategy(strategy_key)(params), risk, tz, funding=fb.get(c)) for c in bars_by_coin}
    cash = r["starting_cash"]
    last_close: dict[str, float] = {}
    events = sorted(((b.time, i, c, b) for i, (c, bars) in enumerate(bars_by_coin.items()) for b in bars),
                    key=lambda e: (e[0], e[1]))
    trades, equity, blocked = [], [], 0
    bars_total = in_pos = 0
    first_open, last_bar_time = {}, None
    for t, _i, coin, bar in events:
        if end_ts is not None and t >= end_ts:
            break
        eng = engines[coin]
        trading = t >= start_ts
        if eng.pending and eng.pending.get("action") == "buy" and not eng.pos:
            if sum(1 for e in engines.values() if e.pos) >= max_open:
                eng.pending = None  # all slots taken
                blocked += 1
        eng.cash = cash
        for tr in eng.process(bar, trading=trading):
            trades.append({**tr, "coin": coin})
        cash = eng.cash
        last_close[coin] = bar.close
        if trading:
            first_open.setdefault(coin, bar.open)
            if last_bar_time != t:  # one equity point per timestamp
                bars_total += 1
                in_pos += any(e.pos for e in engines.values())
                last_bar_time = t
            value = cash + sum(e.position_value(last_close[c]) for c, e in engines.items() if e.pos and c in last_close)
            if equity and equity[-1][0] == bar.end:
                equity[-1] = (bar.end, value)
            else:
                equity.append((bar.end, value))
    if not equity:
        return None
    end_time = equity[-1][0]
    for c, e in engines.items():  # close what is still open at its last price
        if e.pos:
            e.cash = cash
            tr = e.close_all(end_time, last_close[c])
            cash = e.cash
            trades.append({**tr, "coin": c})
    equity[-1] = (end_time, cash)
    bh = [last_close[c] / first_open[c] - 1 for c in first_open if c in last_close]
    metrics = compute_metrics(trades, equity, r["starting_cash"], in_pos, bars_total,
                              buy_hold_pct=sum(bh) / len(bh) * 100 if bh else None)
    metrics["blocked_max_open"] = blocked
    per_coin = {}
    for tr in trades:
        p = per_coin.setdefault(tr["coin"], {"trades": 0, "pnl": 0.0, "wins": 0})
        p["trades"] += 1
        p["pnl"] += tr["pnl"]
        p["wins"] += tr["pnl"] > 0
    return {"trades": trades, "equity": equity, "metrics": metrics, "per_coin": per_coin}


def run_portfolio(run: PortfolioRun) -> PortfolioRun:
    PortfolioRun.objects.filter(pk=run.pk).update(status=PortfolioRun.Status.RUNNING, message="")
    try:
        warm = warmup(run.strategy_key, run.params)
        coins = list(run.coins.all())
        bars = {c.pair: load_bars(c, run.interval, run.start - warm, run.end) for c in coins}
        funding = {c.pair: funding_map(c, run.start - warm, run.end) for c in coins}
        res = simulate_portfolio(bars, run.strategy_key, run.params, run.risk, int(run.start.timestamp()), run.max_open,
                                 funding_by_coin=funding)
        if res is None:
            raise ValueError("No closed candles for these coins in this period. Sync the coins first.")
        every = max(1, math.ceil(len(res["equity"]) / MAX_EQUITY_POINTS))
        eq = res["equity"][::every]
        if eq[-1] != res["equity"][-1]:
            eq.append(res["equity"][-1])
        with transaction.atomic():
            run.metrics, run.per_coin, run.trades = res["metrics"], res["per_coin"], res["trades"]
            run.equity = [[t, round(v, 6)] for t, v in eq]
            run.status, run.finished_at = PortfolioRun.Status.DONE, timezone.now()
            run.save()
        return run
    except Exception as exc:
        PortfolioRun.objects.filter(pk=run.pk).update(status=PortfolioRun.Status.FAILED, message=str(exc)[:4000],
                                                      finished_at=timezone.now())
        raise


def handle_job(job) -> str:
    run = run_portfolio(PortfolioRun.objects.get(pk=job.params["portfolio_id"]))
    return f"Portfolio #{run.pk}: {run.metrics['trades']} trades, {run.metrics['return_pct']:.2f}%"
