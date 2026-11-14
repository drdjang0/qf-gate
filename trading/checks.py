"""Strategy health checks: look-ahead bias and warm-up sufficiency.

Look-ahead (idea from freqtrade's lookahead-analysis, our own implementation):
  1. Database guard: a strategy must decide from `ctx` only. Any database query made inside
     `on_bar` is reported, because it could read candles from the future.
  2. Truncation replay: the strategy is replayed with the future cut off at several points; the
     signal at each cut must equal the signal of the full run at the same bar. Hidden memory
     between runs, randomness or any other leak of later data shows up as a mismatch.

Warm-up (idea from freqtrade's recursive-analysis): the same trading window is run with different
amounts of history before it; if signals inside the window change with more history, the
strategy's `warmup_days` is too short.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import connection

from .engine import Bar, Engine
from .strategies.base import get_strategy

DEFAULT_CUTS = 25


class _DbAccess(Exception):
    pass


@contextmanager
def _no_database():
    def blocker(execute, sql, params, many, context):
        raise _DbAccess(sql[:120])

    with connection.execute_wrapper(blocker):
        yield


def _sig_key(sig) -> tuple | None:
    if sig is None:
        return None
    r = lambda v: None if v is None else round(v, 8)  # noqa: E731
    return (sig.action, r(sig.stop), r(sig.target))


@dataclass
class _Run:
    signals: dict[int, tuple] = field(default_factory=dict)  # bar time -> signal key
    db_queries: list[str] = field(default_factory=list)


def _replay(strategy_key: str, params: dict, bars: list[Bar], risk: dict | None, guard: bool) -> _Run:
    strat = get_strategy(strategy_key)(params)
    run = _Run()
    original = strat.on_bar

    def watched(ctx):
        if not guard:
            sig = original(ctx)
        else:
            try:
                with _no_database():
                    sig = original(ctx)
            except _DbAccess as exc:
                run.db_queries.append(str(exc))
                sig = None
        k = _sig_key(sig)
        if k is not None:
            run.signals[ctx.bar.time] = k
        return sig

    strat.on_bar = watched
    eng = Engine(strat, risk)
    for b in bars:
        eng.process(b)
    return run


def lookahead(strategy_key: str, params: dict, bars: list[Bar], risk: dict | None = None,
              cuts: int = DEFAULT_CUTS) -> dict:
    """Returns {'ok', 'signals', 'checked', 'mismatches': [...], 'db_queries': [...]}."""
    full = _replay(strategy_key, params, bars, risk, guard=True)
    index = {b.time: i for i, b in enumerate(bars)}
    # cut right at signal bars (where bias matters) plus evenly spread points
    points = sorted({index[t] for t in full.signals} | {int(i * len(bars) / (cuts + 1)) for i in range(1, cuts + 1)})
    if len(points) > cuts:
        step = len(points) / cuts
        points = sorted({points[int(i * step)] for i in range(cuts)})
    mismatches = []
    for i in points:
        cut = _replay(strategy_key, params, bars[: i + 1], risk, guard=False)
        t = bars[i].time
        if cut.signals.get(t) != full.signals.get(t):
            mismatches.append({"time": t, "full_run": full.signals.get(t), "future_cut_off": cut.signals.get(t)})
    db = sorted(set(full.db_queries))
    return {"ok": not mismatches and not db, "signals": len(full.signals), "checked": len(points),
            "mismatches": mismatches[:20], "db_queries": db[:5]}


def warmup(strategy_key: str, params: dict, bars: list[Bar], start_ts: int, risk: dict | None = None) -> dict:
    """Compare signals in [start_ts, end] for several warm-up lengths (days before start_ts)."""
    declared = get_strategy(strategy_key)(params).warmup_days()
    first_day = (start_ts - bars[0].time) // 86400 if bars else 0
    lengths = sorted({max(1, declared // 2), declared, min(first_day, declared * 2), first_day})
    lengths = [d for d in lengths if 0 < d <= first_day]
    runs = {}
    for d in lengths:
        sub = [b for b in bars if b.time >= start_ts - d * 86400]
        sig = _replay(strategy_key, params, sub, risk, guard=False).signals
        runs[d] = {t: k for t, k in sig.items() if t >= start_ts}
    if not runs:
        return {"ok": None, "declared": declared, "rows": [], "message": "Not enough history before the start."}
    ref_days = max(runs)
    ref = runs[ref_days]
    rows = []
    for d in lengths:
        diff = sum(1 for t in set(ref) | set(runs[d]) if ref.get(t) != runs[d].get(t))
        rows.append({"days": d, "signals": len(runs[d]), "differ": diff, "declared": d == declared})
    declared_row = next((r for r in rows if r["declared"]), None)
    ok = declared_row["differ"] == 0 if declared_row else None
    return {"ok": ok, "declared": declared, "reference_days": ref_days, "rows": rows,
            "message": "" if declared_row else f"Only {first_day} days of history before the start; "
                                               f"the strategy asks for {declared}."}


def run_checks(config, days: int = 30) -> dict:
    """Both checks on the config's coin/interval over the last `days` days of stored data."""
    from django.utils import timezone

    from .data import load_bars

    end = timezone.now()
    start = end - timedelta(days=days)
    strat = get_strategy(config.strategy_key)(config.params)
    history = timedelta(days=max(2, strat.warmup_days()) * 2)
    bars = load_bars(config.coin, config.interval, start - history, end)
    trade_bars = [b for b in bars if b.time >= int(start.timestamp())]
    warm_bars = [b for b in bars if b.time >= int((start - timedelta(days=strat.warmup_days())).timestamp())]
    return {
        "window": [start.isoformat(), end.isoformat()],
        "bars": len(trade_bars),
        "lookahead": lookahead(config.strategy_key, config.params, warm_bars, config.risk),
        "warmup": warmup(config.strategy_key, config.params, bars, int(start.timestamp()), config.risk),
    }
