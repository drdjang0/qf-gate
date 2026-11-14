"""Parameter sweeps: backtest every combination of a parameter grid on the same bars.

Bars are loaded once and reused; each combination is a fast in-memory engine run.
The period is split into in-sample (ranking) and out-of-sample (validation).
"""

from __future__ import annotations

import itertools
import logging
import math
import random
import threading
from datetime import timedelta

from django.db import close_old_connections
from django.utils import timezone

from .backtest import simulate, warmup
from market.services.funding import funding_map

from .data import load_bars
from .models import Sweep
from .strategies.base import RISK_PARAMS, Param, get_strategy

log = logging.getLogger(__name__)

MAX_COMBOS = 1000  # runs per sweep (grid: combinations; smart search: budget)
MAX_SPACE = 10**12  # smart search can explore spaces this large
MAX_FOLDS = 8
MAX_VALUES_PER_PARAM = 200
KEEP = ("return_pct", "buy_hold_pct", "max_drawdown_pct", "trades", "win_rate_pct", "sortino", "calmar",
        "profit_factor", "avg_r", "sharpe", "fees")


def param_spec(strategy_key: str) -> dict[str, Param]:
    """All sweepable settings, keyed 'p.<strategy param>' and 'r.<risk param>'."""
    cls = get_strategy(strategy_key)
    return {**{f"p.{k}": p for k, p in cls.params.items()}, **{f"r.{k}": p for k, p in RISK_PARAMS.items()}}


def parse_values(text: str, p: Param, name: str) -> list:
    """'1, 2, 3' | 'start:stop:step' (numbers) | 'all' (choices) | 'both' (yes/no). Empty -> []."""
    text = (text or "").strip()
    if not text:
        return []
    low = text.lower()
    if p.kind == "bool" and low in {"both", "all", "*"}:
        return [True, False]
    if p.kind == "choice" and low in {"all", "*"}:
        return list(p.choices)
    if p.kind in {"float", "int"} and ":" in text:
        parts = [x.strip() for x in text.split(":")]
        if len(parts) != 3:
            raise ValueError(f"{name}: a range needs start:stop:step, e.g. 0.5:3:0.5")
        try:
            a, b, s = (float(x) for x in parts)
        except ValueError:
            raise ValueError(f"{name}: range values must be numbers") from None
        if s <= 0 or b < a:
            raise ValueError(f"{name}: need step > 0 and stop >= start")
        n = math.floor((b - a) / s + 1e-9) + 1
        if n > MAX_VALUES_PER_PARAM:
            raise ValueError(f"{name}: {n} values is too many (max {MAX_VALUES_PER_PARAM})")
        raw = [round(a + i * s, 10) for i in range(n)]
    else:
        raw = [x.strip() for x in text.split(",") if x.strip()]
    out = []
    for v in raw:
        if p.kind == "bool" and isinstance(v, str):
            v = v.lower() in {"1", "true", "yes", "on", "y"}
        if p.kind == "int" and isinstance(v, float) and v != int(v):
            raise ValueError(f"{name}: {v:g} is not a whole number")
        cv = p.clean(name, int(v) if p.kind == "int" and isinstance(v, float) else v)
        if cv not in out:
            out.append(cv)
    return out


def build_grid(strategy_key: str, raw: dict[str, str]) -> dict[str, list]:
    spec = param_spec(strategy_key)
    grid = {}
    for key, text in raw.items():
        if key not in spec:
            raise ValueError(f"Unknown setting {key}")
        values = parse_values(text, spec[key], key.split(".", 1)[1])
        if values:
            grid[key] = values
    return grid


def combo_count(grid: dict[str, list]) -> int:
    return math.prod(len(v) for v in grid.values()) if grid else 1


def varied_keys(grid: dict[str, list]) -> list[str]:
    return [k for k, v in grid.items() if len(v) > 1]


def split_combo(combo: dict, base_params: dict, base_risk: dict) -> tuple[dict, dict]:
    """Apply a combo ({'p.x': .., 'r.y': ..}) on top of the base params/risk."""
    params, risk = dict(base_params), dict(base_risk)
    for k, v in combo.items():
        (params if k.startswith("p.") else risk)[k[2:]] = v
    return params, risk


def score(row: dict, rank_by: str) -> float:
    m = row["is"]
    if rank_by == "return_dd":
        return m["return_pct"] / max(m["max_drawdown_pct"], 0.01)
    if rank_by == "combined":
        # balances profit per unit of drawdown, hit rate and sample size (idea: freqtrade's multi-metric loss)
        win = (m.get("win_rate_pct") or 0) / 100
        sample = min(m["trades"], 30) / 30
        return m["return_pct"] / max(m["max_drawdown_pct"], 1.0) * (0.5 + win) * sample ** 0.5
    v = m.get(rank_by)
    return v if v is not None else NO_SCORE


NO_SCORE = -1e18  # finite on purpose: JSON "-Infinity" is rejected by PostgreSQL jsonb


def rank(rows: list[dict], rank_by: str, min_trades: int) -> list[dict]:
    for r in rows:
        r["enough_trades"] = r["is"]["trades"] >= min_trades
        r["score"] = score(r, rank_by)
    return sorted(rows, key=lambda r: (r["enough_trades"], r["score"]), reverse=True)


def _key(combo: dict) -> tuple:
    return tuple(sorted(combo.items()))


def _evaluator(sweep: Sweep, bars, split_ts, oos_bars, start_ts, funding=None):
    def evaluate(combo: dict) -> dict:
        params, risk = split_combo(combo, sweep.base_params, sweep.base_risk)
        is_res = simulate(bars, sweep.strategy_key, params, risk, start_ts, split_ts, funding=funding)
        if is_res is None:
            raise ValueError(f"No closed {sweep.interval} candles for {sweep.config.coin.pair} in this period. "
                             "Sync the coin first.")
        oos_res = simulate(oos_bars, sweep.strategy_key, params, risk, split_ts, funding=funding) if split_ts else None
        return {"combo": combo, "is": {k: is_res[2][k] for k in KEEP},
                "oos": {k: oos_res[2][k] for k in KEEP} if oos_res else None}
    return evaluate


def _neighbours(combo: dict, grid: dict, rng: random.Random) -> list[dict]:
    """Settings one step away: the next value up/down for ordered settings, any other value for choices."""
    out = []
    for k, values in grid.items():
        if len(values) < 2:
            continue
        i = values.index(combo[k])
        if isinstance(values[0], str):
            options = [v for v in values if v != combo[k]]
        else:
            options = [values[j] for j in (i - 1, i + 1) if 0 <= j < len(values)]
        for v in options:
            out.append({**combo, k: v})
    rng.shuffle(out)
    return out


def smart_search(grid: dict, budget: int, evaluate, rank_by: str, min_trades: int, seed: int = 0,
                 progress=None) -> list[dict]:
    """Random sampling (40% of the budget), then repeated refinement around the best settings found.
    Deterministic for a given seed. Returns the evaluated rows (unranked)."""
    rng = random.Random(seed)
    keys = list(grid)
    space = combo_count(grid)
    budget = min(budget, space)
    seen: dict[tuple, dict] = {}

    def run(combo):
        k = _key(combo)
        if k not in seen and len(seen) < budget:
            seen[k] = evaluate(combo)
            if progress:
                progress(len(seen))

    def random_combo():
        return {k: rng.choice(grid[k]) for k in keys}

    tries = 0
    while len(seen) < max(1, int(budget * 0.4)) and tries < budget * 50:
        run(random_combo())
        tries += 1
    stale = 0
    while len(seen) < budget and stale < 3:
        before = len(seen)
        best = rank(list(seen.values()), rank_by, min_trades)[:5]
        for row in best:
            for nb in _neighbours(row["combo"], grid, rng)[:6]:
                run(nb)
        if len(seen) == before:
            stale += 1
            for _ in range(10):  # nothing new near the best: explore elsewhere
                run(random_combo())
        else:
            stale = 0
    return list(seen.values())


def walk_forward(sweep: Sweep, combos: list[dict], bars, warm: timedelta, folds: int, progress=None) -> dict:
    """Anchored walk-forward: the period is cut into folds + 1 equal parts; in fold i the best settings
    on everything before part i (in-sample) are picked, then run on part i (never seen before)."""
    start, end = sweep.start, sweep.end
    edges = [start + (end - start) * j / (folds + 1) for j in range(folds + 2)]
    done = 0
    out, compounded = [], 1.0
    for i in range(1, folds + 1):
        train_start, train_end, test_end = edges[0], edges[i], edges[i + 1]
        t0, t1, t2 = (int(x.timestamp()) for x in (train_start, train_end, test_end))
        train_bars = [b for b in bars if b.time < t1]
        test_bars = [b for b in bars if t1 - warm.total_seconds() <= b.time < t2]
        rows = []
        for combo in combos:
            params, risk = split_combo(combo, sweep.base_params, sweep.base_risk)
            res = simulate(train_bars, sweep.strategy_key, params, risk, t0, t1)
            if res is not None:
                rows.append({"combo": combo, "is": {k: res[2][k] for k in KEEP}})
            done += 1
            if progress and done % 20 == 0:
                progress(done)
        ranked = rank(rows, sweep.rank_by, sweep.min_trades)
        if not ranked:
            continue
        best = ranked[0]
        params, risk = split_combo(best["combo"], sweep.base_params, sweep.base_risk)
        test = simulate(test_bars, sweep.strategy_key, params, risk, t1, t2)
        tm = {k: test[2][k] for k in KEEP} if test else None
        if tm:
            compounded *= 1 + tm["return_pct"] / 100
        out.append({"fold": i, "train": [train_start.isoformat(), train_end.isoformat()],
                    "test": [train_end.isoformat(), test_end.isoformat()], "combo": best["combo"],
                    "train_metrics": best["is"], "enough_trades": best["enough_trades"], "test_metrics": tm})
    tested = [f for f in out if f["test_metrics"]]
    return {
        "folds": out,
        "oos_return_pct": (compounded - 1) * 100,
        "positive_folds": sum(1 for f in tested if f["test_metrics"]["return_pct"] > 0),
        "tested_folds": len(tested),
        "same_settings": len({_key(f["combo"]) for f in out}) == 1 if out else None,
    }


def run_sweep(sweep: Sweep) -> Sweep:
    grid = sweep.grid or {}
    smart = sweep.method == "smart"
    folds = min(sweep.wf_folds or 0, MAX_FOLDS)
    if smart:
        if combo_count(grid) > MAX_SPACE:
            raise ValueError(f"The search space is too large (max {MAX_SPACE:,} combinations)")
        runs = min(sweep.budget or 200, MAX_COMBOS, combo_count(grid))
        combos = None
    else:
        combos = [dict(zip(grid, vals)) for vals in itertools.product(*grid.values())] if grid else [{}]
        if len(combos) > MAX_COMBOS:
            raise ValueError(f"{len(combos)} combinations is too many (max {MAX_COMBOS})")
        runs = len(combos)
    total = runs * (1 + folds)
    Sweep.objects.filter(pk=sweep.pk).update(status=Sweep.Status.RUNNING, total=total, done=0, message="")
    try:
        coin = sweep.config.coin
        all_combos = combos or [{k: v[0] for k, v in grid.items()}]
        warm = max(warmup(sweep.strategy_key, split_combo(c, sweep.base_params, sweep.base_risk)[0])
                   for c in (all_combos if not smart else [{k: max(v) if not isinstance(v[0], str) else v[0]
                                                             for k, v in grid.items()}]))
        bars = load_bars(coin, sweep.interval, sweep.start - warm, sweep.end)
        start_ts = int(sweep.start.timestamp())
        split = sweep.split
        split_ts = int(split.timestamp()) if split else None
        oos_bars = [b for b in bars if split_ts and b.time >= split_ts - warm.total_seconds()]
        funding = funding_map(coin, sweep.start - warm, sweep.end)
        evaluate = _evaluator(sweep, bars, split_ts, oos_bars, start_ts, funding)

        def progress(n):
            if n % 10 == 0 or n == runs:
                Sweep.objects.filter(pk=sweep.pk).update(done=n)

        if smart:
            rows = smart_search(grid, runs, evaluate, sweep.rank_by, sweep.min_trades, seed=sweep.pk or 0,
                                progress=progress)
        else:
            rows = []
            for i, combo in enumerate(combos, 1):
                rows.append(evaluate(combo))
                progress(i)

        sweep.results = rank(rows, sweep.rank_by, sweep.min_trades)
        if folds:
            evaluated = [r["combo"] for r in sweep.results]
            sweep.walkforward = walk_forward(
                sweep, evaluated, bars, warm, folds,
                progress=lambda n: Sweep.objects.filter(pk=sweep.pk).update(done=len(rows) + n))
        sweep.total = sweep.done = len(rows) * (1 + folds)
        sweep.status = Sweep.Status.DONE
        sweep.finished_at = timezone.now()
        sweep.save(update_fields=["results", "walkforward", "total", "done", "status", "finished_at"])
        return sweep
    except Exception as exc:
        Sweep.objects.filter(pk=sweep.pk).update(
            status=Sweep.Status.FAILED, message=str(exc)[:4000], finished_at=timezone.now()
        )
        raise


def handle_job(job) -> str:
    sweep = Sweep.objects.select_related("config__coin").get(pk=job.params["sweep_id"])
    sweep = run_sweep(sweep)
    best = sweep.results[0] if sweep.results else None
    tail = f"; best in-sample {best['is']['return_pct']:.2f}%" if best else ""
    return f"Sweep #{sweep.pk}: {sweep.total} combinations{tail}"


def run_in_thread(sweep_id: int) -> None:
    """Fallback when no worker is running (local dev): run the sweep in a background thread."""

    def _target():
        close_old_connections()
        try:
            run_sweep(Sweep.objects.select_related("config__coin").get(pk=sweep_id))
        except Exception:  # noqa: BLE001 - recorded on the sweep
            log.exception("Sweep %s failed", sweep_id)
        finally:
            close_old_connections()

    threading.Thread(target=_target, daemon=True, name=f"sweep-{sweep_id}").start()


def heatmap(sweep: Sweep) -> dict | None:
    """Best in-sample score per (x, y) of the first two varied settings, with its out-of-sample return."""
    keys = varied_keys(sweep.grid)
    if len(keys) < 2 or not sweep.results:
        return None
    xk, yk = keys[0], keys[1]
    xs, ys = sweep.grid[xk], sweep.grid[yk]
    best: dict[tuple, dict] = {}
    for r in sweep.results:
        if not r["enough_trades"]:
            continue
        cell = (r["combo"][xk], r["combo"][yk])
        if cell not in best or r["score"] > best[cell]["score"]:
            best[cell] = r
    top = max((abs(r["is"]["return_pct"]) for r in best.values()), default=1) or 1
    rows = []
    for y in ys:
        cells = []
        for x in xs:
            r = best.get((x, y))
            if r is None:
                cells.append(None)
                continue
            v = r["is"]["return_pct"]
            alpha = min(1.0, abs(v) / top) * 0.55 + 0.08
            color = f"rgba(22,163,74,{alpha:.2f})" if v >= 0 else f"rgba(220,38,38,{alpha:.2f})"
            oos = r["oos"]["return_pct"] if r["oos"] else None
            cells.append({"is": v, "oos": oos, "trades": r["is"]["trades"], "bg": color})
        rows.append({"y": y, "cells": cells})
    return {"x_key": xk, "y_key": yk, "x_label": xk.split(".", 1)[1], "y_label": yk.split(".", 1)[1],
            "xs": xs, "rows": rows}
