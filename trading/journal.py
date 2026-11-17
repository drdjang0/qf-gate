"""Trade journal with reflection (roadmap 7.1, idea from TradingAgents' decision memory, no LLM).

Every closed trade is reviewed with hindsight, on purpose: this is a post-mortem, never an input to a
decision. For each trade we measure

- the excursions while it was open: how far price went in our favour (MFE) and against us (MAE),
  in R (1R = the distance from entry to the initial stop; 1% of the entry when there was no stop);
- what price did in the N bars AFTER the exit bar: how much further it ran our way (missed) or
  against us (avoided), and whether it came back past the entry.

That gives each exit a verdict (exited early, good exit, shaken out, stop saved, ...) and the
summary turns the counts into plain-language lessons with the numbers behind them. A trade whose
N bars after the exit have not all closed yet is "waiting" and settles on its own later.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from market.services.chart import CHART_INTERVALS

HORIZONS = (4, 12, 24, 48, 96)
DEFAULT_HORIZON = 12
MIN_SAMPLE = 5  # fewer relevant trades than this: no lesson drawn from them

STOP_EXITS = {"stop", "trailing stop", "breakeven", "liquidation"}
SKIP_EXITS = {"end of test"}

VERDICTS = {
    "exited early": ("warn", "A winner that kept going at least 1R further after the exit"),
    "good exit": ("ok", "A winner that did not run 1R further after the exit"),
    "shaken out": ("warn", "Stopped out at a loss, then price came back past the entry"),
    "stop saved": ("ok", "Stopped out, and price kept going against the trade by at least 1R more"),
    "fair stop": ("", "Stopped out; price neither recovered nor fell much further"),
    "closed before recovery": ("warn", "Closed at a loss (not by the stop), then price came back past the entry"),
    "cut loss well": ("ok", "Closed at a loss (not by the stop), and price fell at least 1R further"),
    "fair exit": ("", "Closed at a loss (not by the stop); nothing much happened after"),
    "waiting": ("", "Not all of the bars after the exit have closed yet"),
    "test ended": ("", "Closed by the end of the backtest, not by the strategy"),
}


def _ts(x) -> int:
    return x if isinstance(x, int) else int(x.timestamp())


def _get(t, name):
    return t.get(name) if isinstance(t, dict) else getattr(t, name)


def settle(trades, bars, horizon: int = DEFAULT_HORIZON) -> list[dict]:
    """Review each trade against `bars` (closed bars of the strategy's interval, any order)."""
    bars = sorted(bars, key=lambda b: b.time)
    times = [b.time for b in bars]
    out = []
    for t in trades:
        entry_ts, exit_ts = _ts(_get(t, "entry_time")), _ts(_get(t, "exit_time"))
        entry, exit_, stop = float(_get(t, "entry_price")), float(_get(t, "exit_price")), _get(t, "stop")
        d = -1 if (_get(t, "side") or "long") == "short" else 1
        risk = abs(entry - float(stop)) if stop else 0.0
        r_basis = "stop" if risk > 0 else "1% of entry"
        risk = risk if risk > 0 else entry * 0.01
        pnl, reason = float(_get(t, "pnl")), _get(t, "exit_reason") or ""

        inside = bars[bisect_left(times, entry_ts): bisect_right(times, exit_ts)]
        if inside:
            hi, lo = max(b.high for b in inside), min(b.low for b in inside)
            mfe = ((hi - entry) if d > 0 else (entry - lo)) / risk
            mae = ((entry - lo) if d > 0 else (hi - entry)) / risk
        else:
            mfe = mae = None
        after = bars[bisect_right(times, exit_ts):][:horizon]
        if after:
            hi, lo = max(b.high for b in after), min(b.low for b in after)
            run = ((hi - exit_) if d > 0 else (exit_ - lo)) / risk  # further in the trade's direction
            drop = ((exit_ - lo) if d > 0 else (hi - exit_)) / risk  # further against it
            recovered = (hi >= entry) if d > 0 else (lo <= entry)
            close_r = (after[-1].close - exit_) * d / risk
        else:
            run = drop = close_r = None
            recovered = False
        settled = len(after) >= horizon

        if reason in SKIP_EXITS:
            verdict = "test ended"
        elif not settled:
            verdict = "waiting"
        elif pnl > 0:
            verdict = "exited early" if run >= 1 else "good exit"
        elif reason in STOP_EXITS:
            verdict = "shaken out" if recovered else ("stop saved" if drop >= 1 else "fair stop")
        else:
            verdict = "closed before recovery" if recovered else ("cut loss well" if drop >= 1 else "fair exit")

        out.append({
            "entry_time": entry_ts, "exit_time": exit_ts, "side": "short" if d < 0 else "long",
            "entry_dt": datetime.fromtimestamp(entry_ts, tz=timezone.utc),
            "entry_price": entry, "exit_price": exit_, "pnl": pnl, "r": float(_get(t, "r_multiple") or 0),
            "exit_reason": reason or "—", "entry_tag": _get(t, "entry_tag") or "—",
            "risk": risk, "r_basis": r_basis, "mfe_r": mfe, "mae_r": mae,
            "run_after_r": run, "drop_after_r": drop, "close_after_r": close_r, "recovered": recovered,
            "bars_after": len(after), "settled": settled, "verdict": verdict, "tone": VERDICTS[verdict][0],
        })
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _group(rows, key):
    by = defaultdict(list)
    for r in rows:
        by[r[key]].append(r)
    out = []
    for k, rs in by.items():
        wins = [r for r in rs if r["pnl"] > 0]
        out.append({
            "key": k, "trades": len(rs), "win_pct": len(wins) / len(rs) * 100, "avg_r": _avg(r["r"] for r in rs),
            "avg_mfe": _avg(r["mfe_r"] for r in rs), "avg_mae": _avg(r["mae_r"] for r in rs),
            "avg_missed": _avg(r["run_after_r"] for r in wins),
            "early": sum(r["verdict"] == "exited early" for r in rs),
            "shaken": sum(r["verdict"] in ("shaken out", "closed before recovery") for r in rs),
        })
    return sorted(out, key=lambda g: -g["trades"])


def _pct(k, n):
    return f"{k} of {n} ({k / n * 100:.0f}%)"


def summarize(rows: list[dict], horizon: int, interval: str = "") -> dict:
    """Verdict counts, groupings and plain-language lessons (each with the numbers behind it)."""
    done = [r for r in rows if r["settled"] and r["verdict"] != "test ended"]
    waiting = [r for r in rows if r["verdict"] == "waiting"]
    span = f"{horizon} {interval} bars".strip() if interval else f"{horizon} bars"
    lessons: list[dict] = []

    def lesson(tone, text):
        lessons.append({"tone": tone, "text": text})

    wins = [r for r in done if r["pnl"] > 0]
    losses = [r for r in done if r["pnl"] <= 0]
    stopped = [r for r in losses if r["exit_reason"] in STOP_EXITS]
    other_losses = [r for r in losses if r["exit_reason"] not in STOP_EXITS]

    if len(stopped) >= MIN_SAMPLE:
        shaken = sum(r["verdict"] == "shaken out" for r in stopped)
        saved = sum(r["verdict"] == "stop saved" for r in stopped)
        if shaken / len(stopped) >= 0.3:
            lesson("warn", f"{_pct(shaken, len(stopped))} stop-outs came back past the entry within {span}. "
                           "The stop may be too tight: try a wider stop buffer, or sweep it.")
        elif saved / len(stopped) >= 0.5:
            lesson("ok", f"Stops did their job: {_pct(saved, len(stopped))} stop-outs kept going at least 1R further "
                         f"against the trade within {span}.")
    if len(wins) >= MIN_SAMPLE:
        early = [r for r in wins if r["verdict"] == "exited early"]
        if len(early) / len(wins) >= 0.4:
            avg = _avg(r["run_after_r"] for r in early)
            lesson("warn", f"{_pct(len(early), len(wins))} winners kept going at least 1R after the exit "
                           f"(on average +{avg:.1f}R within {span}). Exits may be too early: try a trailing stop, "
                           "a higher target R or a partial take-profit.")
        elif len(early) / len(wins) <= 0.15:
            lesson("ok", f"Profit exits look well placed: only {_pct(len(early), len(wins))} winners ran 1R further.")
    if len(losses) >= MIN_SAMPLE:
        gave_back = [r for r in losses if (r["mfe_r"] or 0) >= 1]
        if len(gave_back) >= 3 and len(gave_back) / len(losses) >= 0.25:
            lesson("warn", f"{_pct(len(gave_back), len(losses))} losing trades were up at least +1R first. "
                           "A break-even rule (move the stop to entry at +1R) would have protected them.")
    if len(other_losses) >= MIN_SAMPLE:
        early_close = sum(r["verdict"] == "closed before recovery" for r in other_losses)
        if early_close / len(other_losses) >= 0.4:
            reasons = ", ".join(sorted({r["exit_reason"] for r in other_losses}))
            lesson("warn", f"{_pct(early_close, len(other_losses))} losses closed by {reasons} came back past the entry "
                           f"within {span}. Those exits may cut trades short.")

    tags = [g for g in _group(done, "entry_tag") if g["trades"] >= 3]
    if len(tags) >= 2:
        best, worst = max(tags, key=lambda g: g["avg_r"]), min(tags, key=lambda g: g["avg_r"])
        lesson("info", f"Best entry type: {best['key']} (average {best['avg_r']:+.2f}R over {best['trades']} trades); "
                       f"weakest: {worst['key']} ({worst['avg_r']:+.2f}R over {worst['trades']}).")
    exits = [g for g in _group(done, "exit_reason") if g["trades"] >= 3 and g["avg_r"] < 0]
    if exits:
        w = min(exits, key=lambda g: g["avg_r"] * g["trades"])
        lesson("info", f"Most costly exit type: {w['key']} ({w['trades']} trades, average {w['avg_r']:+.2f}R).")
    no_stop = sum(r["r_basis"] != "stop" for r in done)
    if no_stop:
        lesson("info", f"{no_stop} trades had no stop, so their R is measured as 1% of the entry price.")
    if waiting:
        lesson("info", f"{len(waiting)} recent trades are still waiting for {span} after their exit.")
    if done and not any(x["tone"] in ("warn", "ok") for x in lessons):
        lesson("info", f"No pattern stood out over {len(done)} reviewed trades"
                       + (f" (most lessons need at least {MIN_SAMPLE} trades of a kind)." if len(done) < 20 else "."))
    if not done and not waiting:
        lesson("info", "No closed trades to review yet.")

    return {
        "reviewed": len(done), "waiting": len(waiting), "horizon": horizon, "span": span,
        "verdicts": [{"name": v, "count": c, "tone": VERDICTS[v][0], "help": VERDICTS[v][1]}
                     for v, c in Counter(r["verdict"] for r in rows).most_common()],
        "avg_mfe_win": _avg(r["mfe_r"] for r in wins), "avg_mae_win": _avg(r["mae_r"] for r in wins),
        "avg_mfe_loss": _avg(r["mfe_r"] for r in losses), "avg_mae_loss": _avg(r["mae_r"] for r in losses),
        "by_exit": _group(done, "exit_reason"), "by_tag": _group(done, "entry_tag"),
        "lessons": lessons,
    }


def build(coin, interval: str, trades, horizon: int = DEFAULT_HORIZON, cache_key: str | None = None) -> dict:
    """Load the bars the trades need (plus `horizon` bars after the last exit) and review them."""
    from .data import load_bars  # late import: keeps settle/summarize free of the DB layer for tests

    horizon = horizon if horizon in HORIZONS else DEFAULT_HORIZON
    trades = list(trades)
    if cache_key:
        key = f"journal:{cache_key}:{horizon}:{len(trades)}"
        hit = cache.get(key)
        if hit is not None:
            return hit
    if not trades:
        rows = []
    else:
        step = CHART_INTERVALS[interval]
        start = min(_get(t, "entry_time") for t in trades) - timedelta(seconds=step)
        end = max(_get(t, "exit_time") for t in trades) + timedelta(seconds=step * (horizon + 2))
        rows = settle(trades, load_bars(coin, interval, start, end, detail=False), horizon)
    result = {"rows": rows, "summary": summarize(rows, horizon, interval), "horizon": horizon,
              "horizons": HORIZONS, "interval": interval}
    if cache_key:
        # a finished set (nothing waiting) never changes; otherwise refresh soon
        cache.set(key, result, 3600 if not result["summary"]["waiting"] else 60)
    return result
