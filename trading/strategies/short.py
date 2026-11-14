"""Short versions of every long strategy, by mirroring prices.

A long strategy is run on inverted prices (p -> 1/p): highs become lows, a breakout above the previous
day's high becomes a breakdown below the previous day's low, a bounce off support becomes a rejection
from resistance, and taker buys become taker sells. Its "buy" (stop below) is translated back into a
"short" (stop above) at the real prices, "sell" into "cover". Percentage settings mean the same on both
sides, so every built-in long strategy gets a short twin without a second implementation.

Shorts need the futures market (risk setting `market = futures`); on spot they are blocked by the engine.
"""

from __future__ import annotations

from dataclasses import replace

from .base import Context, Signal, Strategy


def _inv(x):
    return None if x is None or x == 0 else 1.0 / x


def _inv_bar(b):
    return replace(b, open=_inv(b.open), high=_inv(b.low), low=_inv(b.high), close=_inv(b.close), detail=None,
                   buy_vol=b.sell_vol, sell_vol=b.buy_vol)


def _inv_day(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {**d, "open": _inv(d["open"]), "high": _inv(d["low"]), "low": _inv(d["high"]), "close": _inv(d["close"])}


def _inv_pos(p: dict | None) -> dict | None:
    if p is None:
        return None
    q = dict(p)
    for k in ("entry_price", "stop", "target", "initial_stop", "last_fill", "liq"):
        if q.get(k) is not None:
            q[k] = _inv(q[k])
    q["high"], q["low"] = _inv(p.get("low")), _inv(p.get("high"))
    q["side"] = 1  # the inner strategy always believes it is long
    return q


def make_short(long_cls: type[Strategy]) -> type[Strategy]:
    class Short(Strategy):
        key = f"{long_cls.key}_short"
        name = long_cls.name.replace("(long)", "").strip() + " (short, mirrored)"
        description = ("Mirror of: " + long_cls.description + " Runs the long logic on inverted prices and "
                       "trades the opposite way. Needs market = futures.")
        params = long_cls.params
        lookback = long_cls.lookback

        def __init__(self, params: dict | None = None):
            super().__init__(params)
            self.inner = long_cls(params)

        def warmup_days(self) -> int:
            return self.inner.warmup_days()

        def _ctx(self, ctx: Context) -> Context:
            return Context(
                bar=_inv_bar(ctx.bar), index=ctx.index, bars=[_inv_bar(b) for b in ctx.bars],
                day=_inv_day(ctx.day), prev_day=_inv_day(ctx.prev_day), is_new_day=ctx.is_new_day,
                is_last_bar_of_day=ctx.is_last_bar_of_day, position=_inv_pos(ctx.position), state=ctx.state,
                params=self.inner.p,
            )

        def on_bar(self, ctx: Context) -> Signal | None:
            sig = self.inner.on_bar(self._ctx(ctx))
            if sig is None:
                return None
            if sig.action == "buy":
                return Signal("short", sig.reason.replace("buy", "short"), stop=_inv(sig.stop),
                              target=_inv(sig.target), tag=f"{sig.tag or long_cls.key}_short", limit=_inv(sig.limit))
            return Signal("cover", sig.reason)

        def plot(self, ctx: Context) -> dict:
            names = {"PDH": "PDL", "PDL": "PDH", "zone support": "zone resistance", "zone resistance": "zone support"}
            return {names.get(k, k): _inv(v) for k, v in self.inner.plot(self._ctx(ctx)).items()}

    Short.__name__ = Short.__qualname__ = f"{long_cls.__name__}Short"
    return Short
