"""Built-in strategies around the previous day's high (PDH) and low (PDL).

Both are long-only (Gate.io spot). Short versions belong to futures (phase 4+).
"""

from __future__ import annotations

from .base import Context, Param, Signal, Strategy, register


def _day_exit(ctx: Context, p: dict) -> Signal | None:
    if ctx.position and p.get("exit_at_day_end") and ctx.is_last_bar_of_day:
        return Signal("sell", "day end")
    return None


def _target(entry_ref: float, stop: float, ctx: Context, p: dict) -> float | None:
    mode = p["target_mode"]
    if mode == "r":
        return entry_ref + (entry_ref - stop) * p["target_r"]
    if mode == "pdh" and ctx.pdh and ctx.pdh > entry_ref:
        return ctx.pdh
    if mode == "mid" and ctx.pdh and ctx.pdl:
        mid = (ctx.pdh + ctx.pdl) / 2
        return mid if mid > entry_ref else None
    return None


def _pd_plot(self, ctx: Context) -> dict[str, float | None]:
    return {"PDH": ctx.pdh, "PDL": ctx.pdl}


@register
class PdhBreakout(Strategy):
    plot = _pd_plot
    key = "pdh_breakout"
    name = "PDH breakout (long)"
    description = (
        "Buys when a bar closes above the previous day's high (PDH) after closing at or below it. "
        "Stop just under PDH (or under the breakout bar's low); target a multiple of the risk."
    )
    params = {
        "buffer_pct": Param("float", 0.05, "Close must exceed PDH by this %, to skip marginal pokes", min=0, max=10),
        "stop_mode": Param("choice", "pdh", "Stop below PDH or below the breakout bar's low", choices=("pdh", "bar_low")),
        "stop_buffer_pct": Param("float", 0.3, "Extra distance below the stop level, %", min=0, max=20),
        "target_mode": Param("choice", "r", "Take profit at an R multiple, or none", choices=("r", "none")),
        "target_r": Param("float", 2.0, "Target as a multiple of the risk (entry - stop)", min=0.1, max=50),
        "one_trade_per_day": Param("bool", True, "At most one entry per day"),
        "exit_at_day_end": Param("bool", True, "Close any open position at the end of the day"),
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p, bar = self.p, ctx.bar
        if (s := _day_exit(ctx, p)) is not None:
            return s
        if ctx.position or ctx.pdh is None or len(ctx.bars) < 2:
            return None
        if p["one_trade_per_day"] and ctx.state.get("traded_day") == ctx.day["date"]:
            return None
        if p["exit_at_day_end"] and ctx.is_last_bar_of_day:
            return None  # would be closed again immediately
        level = ctx.pdh * (1 + p["buffer_pct"] / 100)
        prev_close = ctx.bars[-2].close
        if bar.close > level and prev_close <= level:
            base = ctx.pdh if p["stop_mode"] == "pdh" else bar.low
            stop = base * (1 - p["stop_buffer_pct"] / 100)
            if stop >= bar.close:
                return None
            ctx.state["traded_day"] = ctx.day["date"]
            target = _target(bar.close, stop, ctx, p) if p["target_mode"] == "r" else None
            return Signal("buy", f"close {bar.close:g} > PDH {ctx.pdh:g}", stop=stop, target=target, tag="pdh_breakout")
        return None


@register
class PdlReclaim(Strategy):
    plot = _pd_plot
    key = "pdl_reclaim"
    name = "PDL sweep & reclaim (long)"
    description = (
        "Waits for price to trade below the previous day's low (a liquidity sweep), then buys when a bar "
        "closes back above PDL within a few bars. Stop under the sweep low; target R multiple, mid-range or PDH."
    )
    params = {
        "sweep_pct": Param("float", 0.05, "How far below PDL the low must go to count as a sweep, %", min=0, max=20),
        "max_bars_below": Param("int", 4, "Reclaim must happen within this many bars of the sweep", min=1, max=500),
        "stop_buffer_pct": Param("float", 0.2, "Stop distance below the sweep low, %", min=0, max=20),
        "target_mode": Param("choice", "r", "Target: R multiple, middle of previous day, or PDH", choices=("r", "mid", "pdh")),
        "target_r": Param("float", 2.0, "Target as a multiple of the risk (used when target is R)", min=0.1, max=50),
        "one_trade_per_day": Param("bool", True, "At most one entry per day"),
        "exit_at_day_end": Param("bool", True, "Close any open position at the end of the day"),
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p, bar, st = self.p, ctx.bar, ctx.state
        if (s := _day_exit(ctx, p)) is not None:
            return s
        if ctx.is_new_day:
            st.pop("sweep_low", None)
            st.pop("sweep_index", None)
        if ctx.pdl is None:
            return None
        if bar.low < ctx.pdl * (1 - p["sweep_pct"] / 100):
            st["sweep_low"] = min(st.get("sweep_low", bar.low), bar.low)
            st.setdefault("sweep_index", ctx.index)
        if ctx.position or "sweep_low" not in st:
            return None
        if ctx.index - st["sweep_index"] > p["max_bars_below"]:
            st.pop("sweep_low")
            st.pop("sweep_index")
            return None
        if p["one_trade_per_day"] and st.get("traded_day") == ctx.day["date"]:
            return None
        if p["exit_at_day_end"] and ctx.is_last_bar_of_day:
            return None
        if bar.close > ctx.pdl:
            stop = st["sweep_low"] * (1 - p["stop_buffer_pct"] / 100)
            if stop >= bar.close:
                return None
            target = _target(bar.close, stop, ctx, p)
            st["traded_day"] = ctx.day["date"]
            st.pop("sweep_low")
            st.pop("sweep_index")
            return Signal("buy", f"reclaimed PDL {ctx.pdl:g} after sweep", stop=stop, target=target)
        return None
