"""Strategy plug-in API.

A strategy is a small class that looks at each *closed* bar and may return a
Signal. It never places orders itself: the engine turns signals into fills at
the NEXT bar's open (no look-ahead), sizes the position from the risk
settings, and enforces stop-loss / take-profit. Because the same engine runs
backtests, paper trading and (phase 4) live trading, a strategy behaves the
same everywhere.

To add a strategy: subclass Strategy, set `key`, `name`, `description`,
`params`, implement `on_bar`, and decorate the class with @register.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Param:
    kind: str  # "float" | "int" | "bool" | "choice"
    default: Any
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()

    def clean(self, name: str, value: Any) -> Any:
        if value is None or value == "":
            return self.default
        try:
            if self.kind == "bool":
                v = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "on", "yes"}
            elif self.kind == "int":
                v = int(value)
            elif self.kind == "float":
                v = float(value)
            elif self.kind == "choice":
                v = str(value)
                if v not in self.choices:
                    raise ValueError(f"must be one of {', '.join(self.choices)}")
            else:  # pragma: no cover
                raise ValueError(f"unknown kind {self.kind}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: {exc}") from exc
        if self.min is not None and v < self.min:
            raise ValueError(f"{name}: must be >= {self.min}")
        if self.max is not None and v > self.max:
            raise ValueError(f"{name}: must be <= {self.max}")
        return v


def clean_params(spec: dict[str, Param], values: dict | None) -> dict:
    values = values or {}
    return {name: p.clean(name, values.get(name)) for name, p in spec.items()}


# Risk / execution settings shared by every strategy.
RISK_PARAMS: dict[str, Param] = {
    "starting_cash": Param("float", 10_000.0, "Starting balance in the quote currency (e.g. USDT)", min=1),
    "risk_pct": Param("float", 1.0, "Max loss per trade if the stop is hit, % of equity", min=0.01, max=100),
    "max_position_pct": Param("float", 100.0, "Max position size, % of equity", min=1, max=100),
    "fee_rate": Param("float", 0.002, "Fee per fill as a fraction (Gate spot default 0.2% = 0.002)", min=0, max=0.05),
    "slippage_bps": Param("float", 2.0, "Assumed slippage per fill, basis points", min=0, max=500),
    # -- market: spot (long only) or futures (long + short, leverage, funding, liquidation)
    "market": Param("choice", "spot", "spot = long only, pay the full amount; futures = long and short with "
                    "margin, funding every 8 h and a liquidation price (perpetual priced from spot candles)",
                    choices=("spot", "futures")),
    "leverage": Param("float", 1.0, "Futures: leverage (margin = position value / leverage)", min=1, max=125),
    "maint_margin_rate": Param("float", 0.003, "Futures: maintenance margin rate (Gate BTC_USDT: 0.003)",
                               min=0, max=0.5),
    "funding_default": Param("float", 0.0001, "Futures: funding rate per 8 h used where Gate's history has no "
                             "value (it keeps 180 days)", min=-0.01, max=0.01),
    # -- position sizing (the max position % and the available cash always cap the size)
    "size_mode": Param("choice", "risk", "risk = lose risk_pct at the stop; fixed = a fixed amount; "
                       "equity_pct = a % of equity", choices=("risk", "fixed", "equity_pct")),
    "size_value": Param("float", 0.0, "fixed: amount in the quote currency; equity_pct: % of equity", min=0),
    "add_max": Param("int", 0, "Scale in: add to the position at most this many times (0 = off) ...", min=0, max=20),
    "add_step_pct": Param("float", 2.0, "... each time price falls this % below the last fill ...", min=0.01, max=90),
    "add_size_pct": Param("float", 100.0, "... buying this % of the first fill's size (limit fill, maker fee)",
                          min=1, max=1000),
    # -- order types
    "entry_order": Param("choice", "market", "Entry order: market (next open) or limit (maker, fills only if price "
                         "trades through the limit)", choices=("market", "limit")),
    "limit_offset_pct": Param("float", 0.0, "Limit entry: price this % below the signal bar's close", min=0, max=50),
    "limit_expiry_bars": Param("int", 3, "Limit entry: cancel if not filled within this many bars", min=1, max=10_000),
    "maker_fee_rate": Param("float", 0.002, "Fee for limit (maker) fills as a fraction. Set your Gate maker rate.",
                            min=0, max=0.05),
    "exit_targets_maker": Param("bool", False, "Book target / ROI / partial exits as resting limit orders (maker fee)"),
    # -- exit management (0 = off), applied by the engine to every strategy
    "trail_pct": Param("float", 0.0, "Exit: trailing stop this % below the highest price since entry", min=0, max=50),
    "trail_activate_r": Param("float", 0.0, "... only once price has reached +N R (0 = from entry)", min=0, max=50),
    "breakeven_r": Param("float", 0.0, "Exit: move the stop to break-even (entry + fees) once price reaches +N R",
                         min=0, max=50),
    "partial_tp_r": Param("float", 0.0, "Exit: take partial profit at +N R ...", min=0, max=50),
    "partial_tp_pct": Param("float", 50.0, "... selling this % of the position", min=1, max=99),
    "roi_pct": Param("float", 0.0, "Exit: take profit once the gain reaches this % ...", min=0, max=1000),
    "roi_decay_bars": Param("int", 0, "... falling linearly to 0% (break-even) over this many bars (0 = fixed)",
                            min=0, max=100_000),
    "max_hold_bars": Param("int", 0, "Exit: close at the next open after holding this many bars", min=0, max=100_000),
    # -- order-flow entry filter (0 = off)
    "min_entry_delta_pct": Param("float", 0.0, "Order flow: only buy when taker-buy minus taker-sell volume on the "
                                 "signal bar is at least this % of its volume (bars without order-flow data are skipped)",
                                 min=0, max=100),
    # -- 7.7 AI filter (paper trading and dry-run bots only; ignored by backtests, sweeps and live bots)
    "ai_min_rating": Param("choice", "off", "AI filter (paper / dry-run only, not backtestable): only enter when the latest "
                           "AI brief for this coin is at least this rating. Acts only once the brief scorecard has earned "
                           "it (Intel page)", choices=("off", "buy", "overweight", "hold")),
    "ai_max_age_hours": Param("int", 24, "... and that brief is no older than this many hours at the bar's close",
                              min=1, max=720),
    # -- protections (0 = off): pause new entries after bad runs; the base of the live kill switch
    "cooldown_bars": Param("int", 0, "Protection: bars to wait after any exit before a new entry", min=0, max=10_000),
    "sl_guard_trades": Param("int", 0, "Protection: pause after this many stop-outs ...", min=0, max=100),
    "sl_guard_window_bars": Param("int", 96, "... within this many bars ...", min=1, max=100_000),
    "sl_guard_pause_bars": Param("int", 96, "... for this many bars", min=1, max=100_000),
    "max_dd_pct": Param("float", 0.0, "Protection: pause when closed-trade drawdown from the peak reaches this %",
                        min=0, max=100),
    "max_dd_pause_bars": Param("int", 96, "... for this many bars (the peak resets after the pause)", min=1, max=100_000),
    "low_profit_trades": Param("int", 0, "Protection: look at the last N closed trades ...", min=0, max=100),
    "low_profit_min_pct": Param("float", 0.0, "... and pause if their summed return is below this %", min=-100, max=100),
    "low_profit_pause_bars": Param("int", 96, "... for this many bars", min=1, max=100_000),
}
PROTECTION_KEYS = ("cooldown_bars", "sl_guard_trades", "sl_guard_window_bars", "sl_guard_pause_bars", "max_dd_pct",
                   "max_dd_pause_bars", "low_profit_trades", "low_profit_min_pct", "low_profit_pause_bars")


@dataclass
class Signal:
    action: str  # "buy" (open long) | "short" (open short, futures) | "sell" / "cover" (close the position)
    reason: str = ""
    stop: float | None = None  # required for "buy": defines risk and position size
    target: float | None = None
    tag: str = ""  # short entry type for reports, e.g. "pdh_breakout"; defaults to the strategy key
    limit: float | None = None  # "buy" only: a limit price (maker order) instead of buying at the next open


@dataclass
class Context:
    bar: Any  # engine.Bar (closed)
    index: int  # bar counter since the engine started
    bars: list  # recent closed bars, oldest first, includes `bar`
    day: dict  # current day so far: date, open, high, low, close
    prev_day: dict | None  # previous complete day: date, open, high, low, close
    is_new_day: bool  # `bar` is the first bar of its day
    is_last_bar_of_day: bool
    position: dict | None  # qty, entry_price, entry_time, stop, target
    state: dict  # strategy's own persistent, JSON-serialisable state
    params: dict = field(default_factory=dict)

    @property
    def pdh(self) -> float | None:
        return self.prev_day["high"] if self.prev_day else None

    @property
    def pdl(self) -> float | None:
        return self.prev_day["low"] if self.prev_day else None


class Strategy:
    key: str = ""
    name: str = ""
    description: str = ""
    params: dict[str, Param] = {}
    lookback: int = 50  # how many recent bars the context keeps

    def __init__(self, params: dict | None = None):
        self.p = clean_params(self.params, params)

    def warmup_days(self) -> int:
        """Days of bars to replay before trading starts, so the strategy's levels/state are ready."""
        return 2

    def plot(self, ctx: Context) -> dict[str, float | None]:
        """Optional lines to draw on backtest charts: {name: value} for this closed bar (after on_bar)."""
        return {}

    def on_bar(self, ctx: Context) -> Signal | None:  # pragma: no cover - abstract
        raise NotImplementedError


REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not cls.key or cls.key in REGISTRY:
        raise ValueError(f"Strategy key missing or duplicated: {cls.key!r}")
    REGISTRY[cls.key] = cls
    return cls


def _load_builtins() -> None:
    from . import follow, ml, pdh_pdl, zones  # noqa: F401  (importing registers the built-in strategies)
    from .short import make_short

    for key in ("pdh_breakout", "pdl_reclaim", "zone_hotspot", "ml_logit"):  # a mirrored short twin of each long built-in
        if f"{key}_short" not in REGISTRY and key in REGISTRY:
            register(make_short(REGISTRY[key]))


def get_strategy(key: str) -> type[Strategy]:
    _load_builtins()
    try:
        return REGISTRY[key]
    except KeyError:
        raise ValueError(f"Unknown strategy {key!r}") from None


def strategy_choices() -> list[tuple[str, str]]:
    _load_builtins()
    return [(k, c.name) for k, c in REGISTRY.items()]
