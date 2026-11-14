"""Strategies that trade the zone hotspots from the Zones page (long-only spot).

No look-ahead: the strategy records each *completed* day in its own state and, at the start of
every day, recomputes the hotspot bands from those days only, with the same code the Zones page
uses (market/services/zones.py). So a backtest, a sweep and paper trading all see exactly the
bands that were knowable at that moment.

Entry modes:
- bounce:   price comes down into a band from above and a bar closes back above it (support held);
- breakout: a bar closes above a band that price was at or below (resistance gave way).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from market.services.zones import Day, build_zones, hotspots

from .base import Context, Param, Signal, Strategy, register


@lru_cache(maxsize=8192)
def _bands(days: tuple[tuple], width: float, top: int) -> tuple[tuple[float, float, int, int, int], ...]:
    """Hotspot bands (lower, upper, touches, held, broke) from completed days, best first.
    Cached: every combination of a sweep replays the same days."""
    ds: list[Day] = []
    for d, o, h, low, c in days:
        dd = date.fromisoformat(d)
        prev = ds[-1].close if ds and ds[-1].date == dd - timedelta(days=1) else None
        ds.append(Day(dd, o, h, low, c, True, prev))
    levels = [(k, d.date, getattr(d, k), None) for d in ds for k in ("high", "low")]
    zs = build_zones(ds, levels, width)
    return tuple((b["lower"], b["upper"], b["touches"], b["held"], b["broke"]) for b in hotspots(ds, zs, width, top))


def _fmt(x: float) -> str:
    return f"{x:,.2f}" if x >= 1 else f"{x:.8g}"


@register
class ZoneHotspot(Strategy):
    key = "zone_hotspot"
    name = "Zone hotspot bounce / breakout (long)"
    description = (
        "Builds the Zones-page hotspots (bands price kept returning to) from the previous N completed days, "
        "then buys a bounce off a band from above (support) or a close above a band (breakout). "
        "Stop under the band; target the next band above or an R multiple."
    )
    params = {
        "entry_mode": Param("choice", "bounce", "bounce = support held; breakout = closed above the band",
                            choices=("bounce", "breakout")),
        "lookback_days": Param("int", 30, "Completed days used to find the bands", min=3, max=120),
        "width_pct": Param("float", 1.0, "Zone / band width, ± %", min=0.1, max=5),
        "top_bands": Param("int", 8, "How many hotspot bands to use", min=1, max=20),
        "min_touches": Param("int", 3, "A band must have been revisited on at least this many days", min=1, max=100),
        "min_held": Param("int", 1, "...and have held at least this many times", min=0, max=100),
        "confirm_bars": Param("int", 4, "Bounce: a close back above the band must come within this many bars",
                              min=1, max=200),
        "break_pct": Param("float", 0.3, "Bounce is cancelled if a bar closes this % below the band", min=0, max=20),
        "breakout_pct": Param("float", 0.05, "Breakout: close must be this % above the band", min=0, max=10),
        "stop_buffer_pct": Param("float", 0.3, "Stop distance below the band (or the dip low), %", min=0, max=20),
        "target_mode": Param("choice", "next_band", "Target the next band above (falls back to R) or an R multiple",
                             choices=("next_band", "r")),
        "target_r": Param("float", 2.0, "R multiple for the target (entry - stop = 1R)", min=0.1, max=50),
        "min_target_r": Param("float", 1.0, "Skip trades whose target is closer than this many R", min=0, max=20),
        "one_trade_per_day": Param("bool", True, "At most one entry per day"),
        "exit_at_day_end": Param("bool", False, "Close any open position at the end of the day"),
    }

    def warmup_days(self) -> int:
        return self.p["lookback_days"] + 1

    # -- state -----------------------------------------------------------------------------
    def _new_day(self, ctx: Context) -> None:
        st, pd = ctx.state, ctx.prev_day
        days = st.setdefault("days", [])
        if pd and (not days or days[-1][0] != pd["date"]):
            days.append([pd["date"], pd["open"], pd["high"], pd["low"], pd["close"]])
            del days[: -self.p["lookback_days"]]
        p = self.p
        st["bands"] = [list(b) for b in _bands(tuple(tuple(d) for d in days), p["width_pct"] / 100, p["top_bands"])]

    def _usable(self, ctx: Context) -> list[list]:
        p = self.p
        return [b for b in ctx.state.get("bands", []) if b[2] >= p["min_touches"] and b[3] >= p["min_held"]]

    def plot(self, ctx: Context) -> dict[str, float | None]:
        """Nearest usable band below / above the close (support / resistance)."""
        close = ctx.bar.close
        bands = self._usable(ctx)
        below = [b[1] for b in bands if b[1] <= close]
        above = [b[0] for b in bands if b[0] > close]
        return {"zone support": max(below) if below else None, "zone resistance": min(above) if above else None}

    # -- signals ---------------------------------------------------------------------------
    def on_bar(self, ctx: Context) -> Signal | None:
        p, st = self.p, ctx.state
        if ctx.is_new_day or "bands" not in st:
            self._new_day(ctx)
        if ctx.position:
            if p["exit_at_day_end"] and ctx.is_last_bar_of_day:
                return Signal("sell", "day end")
            return None
        if len(ctx.bars) < 2 or (p["exit_at_day_end"] and ctx.is_last_bar_of_day):
            return None
        if p["one_trade_per_day"] and st.get("traded_day") == ctx.day["date"]:
            return None
        bands = self._usable(ctx)
        if not bands:
            st.pop("armed", None)
            return None
        prev_close = ctx.bars[-2].close
        if p["entry_mode"] == "breakout":
            return self._breakout(ctx, bands, prev_close)
        return self._bounce(ctx, bands, prev_close)

    def _bounce(self, ctx: Context, bands: list[list], prev_close: float) -> Signal | None:
        p, bar, st = self.p, ctx.bar, ctx.state
        armed = st.get("armed")
        if armed:
            lower = armed["lower"]
            if bar.close < lower * (1 - p["break_pct"] / 100) or ctx.index - armed["since"] > p["confirm_bars"]:
                st.pop("armed")  # the band broke, or the bounce took too long
                armed = None
            else:
                armed["low"] = min(armed["low"], bar.low)
        if armed is None:
            # a dip into a band from above
            hit = next((b for b in bands if prev_close > b[1] and b[0] * (1 - p["break_pct"] / 100) <= bar.low <= b[1]),
                       None)
            if hit is None:
                return None
            armed = st["armed"] = {"lower": hit[0], "upper": hit[1], "touches": hit[2], "held": hit[3],
                                   "low": bar.low, "since": ctx.index}
        if bar.close <= armed["upper"]:
            return None  # still inside / below: wait for the close back above
        st.pop("armed")
        stop = min(armed["lower"], armed["low"]) * (1 - p["stop_buffer_pct"] / 100)
        why = (f"bounce off zone {_fmt(armed['lower'])}–{_fmt(armed['upper'])} "
               f"({armed['touches']} days returned, held {armed['held']})")
        return self._enter(ctx, bands, stop, why, "zone_bounce")

    def _breakout(self, ctx: Context, bands: list[list], prev_close: float) -> Signal | None:
        p, bar = self.p, ctx.bar
        for lower, upper, touches, held, _broke in bands:
            level = upper * (1 + p["breakout_pct"] / 100)
            if prev_close <= level < bar.close:
                stop = lower * (1 - p["stop_buffer_pct"] / 100)
                why = f"close above zone {_fmt(lower)}–{_fmt(upper)} ({touches} days returned, held {held})"
                return self._enter(ctx, bands, stop, why, "zone_breakout")
        return None

    def _enter(self, ctx: Context, bands: list[list], stop: float, why: str, tag: str) -> Signal | None:
        p, close = self.p, ctx.bar.close
        risk = close - stop
        if risk <= 0:
            return None
        target = close + risk * p["target_r"]
        if p["target_mode"] == "next_band":
            above = [b[0] for b in bands if b[0] > close]
            if above:
                target = min(above)
        if target - close < risk * p["min_target_r"]:
            return None  # not enough room to the next band
        ctx.state["traded_day"] = ctx.day["date"]
        return Signal("buy", why, stop=stop, target=target, tag=tag)
