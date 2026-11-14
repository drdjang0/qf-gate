"""Bar-by-bar trading engine shared by backtests and paper trading.

Rules (identical in backtest and paper, so results are comparable):
- The strategy sees a bar only after it has CLOSED.
- A signal is filled at the NEXT bar's open (+/- slippage): no look-ahead.
- Stop-loss and take-profit are checked against each bar's high/low. When the bar
  carries its 1-minute path (`detail`, loaded from the stored 1m candles), the
  minutes are replayed in order, so whichever level price reached first wins.
  Without detail, or when both are touched inside the same minute, the stop is
  assumed to hit first (worst case). A gap through a level fills at the open of
  that bar / minute, not at the level.
- Position size (size_mode): risk-based = risk_pct of equity / (entry - stop) (default),
  a fixed amount, or a % of equity; always capped by max_position_pct of equity and the
  cash available after fees. Optional scaling in adds at fixed steps below the last fill
  (same stop, same caps); the trade then reports the average entry price.
- Spot is long-only. Futures (risk setting `market`) trade long and short with leverage:
  a position ties up margin = value / leverage, has a liquidation price
  entry x (1 -/+ 1/leverage +/- maintenance rate) checked before the stop, and pays or
  receives funding at every 8-hour boundary (00/08/16 UTC) from Gate's real history
  (`funding`), or `funding_default` where none is stored. Fees are charged on both fills.
- Limit (maker) entries: a buy can rest as a limit order (from the signal or the
  `entry_order` setting). It fills only when price trades below the limit, at the limit
  or at the bar's / minute's open if that is lower, without slippage and at the maker fee;
  it expires after `limit_expiry_bars`. Exits at a target / ROI / partial level can be
  booked at the maker fee too (`exit_targets_maker`); stops are always market orders.
- Exit management (all off by default; ideas from freqtrade): trailing stop (optionally
  activated at +N R), break-even stop, partial take-profit, ROI target that can decay to
  break-even over time, and a time exit. Within a bar the stop is checked first, then
  partial / target / ROI; the trailing high and break-even move only after that bar (or
  minute, with 1m detail), so a stop never benefits from a high reached later in the bar.
- Protections (all off by default; idea from freqtrade): cooldown after an exit,
  stoploss guard, max drawdown and low-profit pauses. While paused, entry signals
  are blocked and counted per reason.

All engine state is JSON-serialisable (`state()`), so a paper session can stop
and resume exactly where it left off.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .strategies.base import RISK_PARAMS, Context, Strategy, clean_params


@dataclass(slots=True)
class Bar:
    time: int  # open time, unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    step: int = 60
    # 1-minute path inside the bar: [(time, open, high, low), ...]. Not saved in state.
    detail: list | None = None
    # order flow: taker buy / sell base volume (None when unknown for the whole bar)
    buy_vol: float | None = None
    sell_vol: float | None = None

    @property
    def delta(self) -> float | None:
        """Taker buy minus taker sell volume (positive = buyers were more aggressive)."""
        return None if self.buy_vol is None else self.buy_vol - self.sell_vol

    @property
    def end(self) -> int:
        return self.time + self.step


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


FUNDING_EVERY = 8 * 3600


def size_for(risk: dict, price: float, stop: float, equity: float, cash: float, fee_rate: float) -> float:
    """Position size in base units: the sizing mode, capped by max position % and the cash after fees.
    Shared by the engine and live/dry-run bots so both size identically. On futures the caps apply to
    the margin (value / leverage), so leverage lets the same cash hold a larger position."""
    lev = risk.get("leverage", 1.0) if risk.get("market") == "futures" else 1.0
    if risk["size_mode"] == "fixed" and risk["size_value"] > 0:
        wanted = risk["size_value"] / price
    elif risk["size_mode"] == "equity_pct" and risk["size_value"] > 0:
        wanted = equity * risk["size_value"] / 100 / price
    else:
        wanted = equity * risk["risk_pct"] / 100 / abs(price - stop)
    return max(0.0, min(wanted, equity * risk["max_position_pct"] / 100 * lev / price,
                        cash / (price * (1 / lev + fee_rate))))


class Engine:
    def __init__(self, strategy: Strategy, risk: dict | None, tz_name: str = "UTC", state: dict | None = None,
                 funding: dict[int, float] | None = None):
        self.strategy = strategy
        self.risk = clean_params(RISK_PARAMS, risk)
        self.futures = self.risk["market"] == "futures"
        self.funding = funding or {}  # funding time (unix) -> rate, futures only
        self.tz = ZoneInfo(tz_name)
        st = state or {}
        self.cash: float = st.get("cash", self.risk["starting_cash"])
        self.pos: dict | None = st.get("pos")
        self.pending: dict | None = st.get("pending")
        self.index: int = st.get("index", 0)
        self.day: dict | None = st.get("day")
        self.prev_day: dict | None = st.get("prev_day")
        self.strat_state: dict = st.get("strategy", {})
        self.last_time: int | None = st.get("last_time")
        self.last_close: float | None = st.get("last_close")
        self.skipped: int = st.get("skipped", 0)
        self.expired: int = st.get("expired", 0)  # limit entries that never filled
        self.detail_used: int = st.get("detail_used", 0)  # bars resolved with the 1m path
        # protections
        self.exits: list = st.get("exits", [])  # recent closed trades: [bar index, reason, return %]
        self.lock_until: int = st.get("lock_until", -1)  # no entries while index <= lock_until
        self.lock_reason: str = st.get("lock_reason", "")
        self.peak: float = st.get("peak", self.cash)
        self.blocked: dict = st.get("blocked", {})
        self.sl_from: int = st.get("sl_from", -1)  # stoploss guard / low-profit only count exits after these
        self.lp_from: int = st.get("lp_from", -1)
        self.funding_paid: float = st.get("funding_paid", 0.0)
        self.liquidations: int = st.get("liquidations", 0)
        self.bars: deque[Bar] = deque((Bar(**b) for b in st.get("bars", [])), maxlen=max(2, strategy.lookback))

    # -- persistence ----------------------------------------------------------
    def state(self) -> dict:
        return {
            "cash": self.cash,
            "pos": self.pos,
            "pending": self.pending,
            "index": self.index,
            "day": self.day,
            "prev_day": self.prev_day,
            "strategy": self.strat_state,
            "last_time": self.last_time,
            "last_close": self.last_close,
            "skipped": self.skipped,
            "expired": self.expired,
            "detail_used": self.detail_used,
            "exits": self.exits,
            "lock_until": self.lock_until,
            "lock_reason": self.lock_reason,
            "peak": self.peak,
            "blocked": self.blocked,
            "sl_from": self.sl_from,
            "lp_from": self.lp_from,
            "funding_paid": self.funding_paid,
            "liquidations": self.liquidations,
            "bars": [{**asdict(b), "detail": None} for b in self.bars],  # buy/sell volume is kept
        }

    def position_value(self, price: float) -> float:
        """What the open position is worth to the account at `price` (margin + unrealised P&L)."""
        p = self.pos
        if not p:
            return 0.0
        d = p.get("side", 1)
        return p.get("margin", p["qty"] * p["entry_price"]) + d * p["qty"] * (price - p["entry_price"])

    def equity(self, price: float | None = None) -> float:
        price = self.last_close if price is None else price
        return self.cash + (self.position_value(price) if self.pos and price is not None else 0.0)

    # -- day tracking -----------------------------------------------------------
    def _day_of(self, ts: int) -> date:
        return datetime.fromtimestamp(ts, tz=self.tz).date()

    def _day_end(self, d: date) -> int:
        nxt = d + timedelta(days=1)
        return int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=self.tz).timestamp())

    def _update_day(self, bar: Bar) -> bool:
        d = self._day_of(bar.time)
        if self.day is None or self.day["date"] != d.isoformat():
            if self.day is not None:
                prev_date = date.fromisoformat(self.day["date"])
                # only a directly preceding day counts as "previous day"
                self.prev_day = dict(self.day) if prev_date == d - timedelta(days=1) else None
            self.day = {"date": d.isoformat(), "open": bar.open, "high": bar.high, "low": bar.low,
                        "close": bar.close, "end": self._day_end(d)}
            return True
        self.day["high"] = max(self.day["high"], bar.high)
        self.day["low"] = min(self.day["low"], bar.low)
        self.day["close"] = bar.close
        return False

    # -- fills ------------------------------------------------------------------
    def _open(self, bar: Bar, sig: dict, price: float | None = None, maker: bool = False) -> None:
        d = -1 if sig["action"] == "short" else 1
        slip = self.risk["slippage_bps"] / 10_000
        fee_rate = self.risk["maker_fee_rate"] if maker else self.risk["fee_rate"]
        price = bar.open * (1 + d * slip) if price is None else price
        stop = sig.get("stop")
        if stop is None or d * (price - stop) <= 0:
            self.skipped += 1  # gapped through the stop before entry: skip
            return
        qty = size_for(self.risk, price, stop, self.cash, self.cash, fee_rate)
        if qty <= 0 or qty * price < 1e-9:
            self.skipped += 1
            return
        lev = self.risk["leverage"] if self.futures else 1.0
        margin = qty * price / lev
        fee = qty * price * fee_rate
        self.cash -= margin + fee
        mmr = self.risk["maint_margin_rate"]
        liq = price * (1 - d / lev + d * mmr) if self.futures and lev > 1 else None
        self.pos = {
            "side": d,
            "margin": margin,
            "liq": liq,
            "funding": 0.0,
            "low": price,  # lowest price since entry (trailing stop of a short)
            "qty": qty,
            "entry_price": price,
            "entry_time": bar.time,
            "stop": stop,
            "target": sig.get("target"),
            "entry_fee": fee,
            "risk_amount": qty * abs(price - stop),
            "entry_reason": sig.get("reason", ""),
            "entry_tag": sig.get("tag") or self.strategy.key,
            "entry_index": self.index,
            "initial_stop": stop,
            "high": price,  # highest price since entry (trailing stop)
            "stop_kind": "stop",
            "partial_done": False,
            "first_qty": qty,
            "last_fill": price,
            "adds": 0,
        }

    def _close(self, ts: int, ref_price: float, reason: str, fraction: float = 1.0) -> dict:
        """Sell `fraction` of the position (1.0 = all) at ref_price minus slippage."""
        slip = self.risk["slippage_bps"] / 10_000
        pos = self.pos
        d = pos.get("side", 1)
        part = fraction < 1.0
        qty = pos["qty"] * fraction
        entry_fee = pos["entry_fee"] * fraction
        risk_amount = pos["risk_amount"] * fraction
        margin = pos.get("margin", pos["qty"] * pos["entry_price"]) * fraction
        funding = pos.get("funding", 0.0) * fraction
        maker = self.risk["exit_targets_maker"] and reason in ("target", "roi", "partial take-profit")
        price = ref_price if maker else ref_price * (1 - d * slip)
        fee = qty * price * (self.risk["maker_fee_rate"] if maker else self.risk["fee_rate"])
        gross = d * qty * (price - pos["entry_price"])
        if reason == "liquidation":
            # isolated margin: liquidated means the whole position margin is gone (the exchange keeps the
            # remaining maintenance margin), but never more than that
            gross = -margin
            self.liquidations += 1
        self.cash += margin + gross - fee
        pnl = gross - fee - entry_fee - funding
        if part:
            pos["qty"] -= qty
            pos["entry_fee"] -= entry_fee
            pos["risk_amount"] -= risk_amount
            pos["margin"] = pos.get("margin", 0) - margin
            pos["funding"] = pos.get("funding", 0.0) - funding
        else:
            self.pos = None
        return {
            "entry_time": pos["entry_time"],
            "entry_price": pos["entry_price"],
            "exit_time": ts,
            "exit_price": price,
            "qty": qty,
            "stop": pos.get("initial_stop", pos["stop"]),
            "target": pos["target"],
            "fees": entry_fee + fee,
            "funding": funding,
            "side": "short" if d < 0 else "long",
            "pnl": pnl,
            "r_multiple": pnl / risk_amount if risk_amount > 0 else 0.0,
            "entry_reason": pos["entry_reason"],
            "entry_tag": pos.get("entry_tag", ""),
            "exit_reason": reason,
            "partial": part,
        }

    # -- main loop --------------------------------------------------------------
    def process(self, bar: Bar, trading: bool = True) -> list[dict]:
        """Feed one CLOSED bar. Returns trades closed during this bar."""
        if self.last_time is not None and bar.time <= self.last_time:
            return []  # already processed (makes paper steps idempotent)
        closed: list[dict] = []

        # 1) fill the order decided at the previous bar's close, at this bar's open (or a resting limit)
        first_seg = 0
        if self.pending:
            sig, self.pending = self.pending, None
            if sig["action"] in ("buy", "short") and not self.pos and trading:
                if sig.get("limit") is None:
                    self._open(bar, sig)
                else:
                    first_seg = self._try_limit(bar, sig)
            elif sig["action"] in ("sell", "cover") and self.pos:
                closed.append(self._close(bar.time, bar.open, sig.get("reason") or "signal"))

        # 2) protective exits inside this bar (stop, partial, target, ROI; trailing / break-even updates)
        if self.pos:
            closed += self._exits(bar, first_seg)
        if self.pos and self.futures:
            self._charge_funding(bar)

        # 3) the strategy sees the closed bar
        is_new_day = self._update_day(bar)
        self.bars.append(bar)
        ctx = Context(
            bar=bar,
            index=self.index,
            bars=list(self.bars),
            day=self.day,
            prev_day=self.prev_day,
            is_new_day=is_new_day,
            is_last_bar_of_day=bar.end >= self.day["end"],
            position=dict(self.pos) if self.pos else None,
            state=self.strat_state,
            params=self.strategy.p,
        )
        for t in closed:
            if not t.get("partial"):
                self._after_exit(t)
        sig = self.strategy.on_bar(ctx)
        if sig is not None and trading:
            if sig.action == "short" and not self.futures:
                self.blocked["short needs futures"] = self.blocked.get("short needs futures", 0) + 1
            elif sig.action in ("buy", "short") and not self.pos and sig.stop is not None:
                if self.index <= self.lock_until:
                    self.blocked[self.lock_reason] = self.blocked.get(self.lock_reason, 0) + 1
                elif not self._flow_ok(bar, -1 if sig.action == "short" else 1):
                    self.blocked["order-flow filter"] = self.blocked.get("order-flow filter", 0) + 1
                elif self.pending is None:  # a resting limit order stays until it fills or expires
                    order = asdict(sig)
                    if order.get("limit") is None and self.risk["entry_order"] == "limit":
                        side = -1 if sig.action == "short" else 1
                        order["limit"] = bar.close * (1 - side * self.risk["limit_offset_pct"] / 100)
                    if order.get("limit") is not None:
                        order["expires"] = self.index + self.risk["limit_expiry_bars"]
                    self.pending = order
            elif sig.action in ("sell", "cover") and self.pos:
                self.pending = asdict(sig)
        hold = self.risk["max_hold_bars"]
        if self.pos and hold and not self.pending and self.index - self.pos.get("entry_index", self.index) + 1 >= hold:
            self.pending = {"action": "sell", "reason": "time exit"}

        self.index += 1
        self.last_time = bar.time
        self.last_close = bar.close
        return closed

    def _flow_ok(self, bar: Bar, side: int = 1) -> bool:
        """Longs need buyers to dominate the signal bar, shorts need sellers to."""
        need = self.risk["min_entry_delta_pct"]
        if not need:
            return True
        if bar.delta is None:
            return False  # no order-flow data for this bar: don't guess
        total = bar.buy_vol + bar.sell_vol
        return total > 0 and side * bar.delta / total * 100 >= need

    def _charge_funding(self, bar: Bar) -> None:
        """Funding at each 8-hour boundary inside this bar: longs pay a positive rate, shorts receive it."""
        t = (bar.time // FUNDING_EVERY + 1) * FUNDING_EVERY
        while t <= bar.end and self.pos:
            rate = self.funding.get(t, self.risk["funding_default"])
            pay = self.pos.get("side", 1) * self.pos["qty"] * bar.close * rate
            self.cash -= pay
            self.pos["funding"] = self.pos.get("funding", 0.0) + pay
            self.funding_paid += pay
            t += FUNDING_EVERY

    def _lock(self, bars: int, reason: str) -> None:
        until = self.index + bars
        if until > self.lock_until:
            self.lock_until, self.lock_reason = until, reason

    def _after_exit(self, trade: dict) -> None:
        """Update protections after a closed trade (evaluated at the close of the exit bar)."""
        r = self.risk
        cost = trade["qty"] * trade["entry_price"] + trade["fees"]
        self.exits.append([self.index, trade["exit_reason"], trade["pnl"] / cost * 100 if cost else 0.0])
        del self.exits[:-100]
        if r["cooldown_bars"]:
            self._lock(r["cooldown_bars"], "cooldown")
        if r["sl_guard_trades"]:
            since = max(self.sl_from, self.index - r["sl_guard_window_bars"])
            if sum(1 for e in self.exits if e[1] == "stop" and e[0] > since) >= r["sl_guard_trades"]:
                self._lock(r["sl_guard_pause_bars"], "stoploss guard")
                self.sl_from = self.index  # start counting again after this
        self.peak = max(self.peak, self.cash)
        if r["max_dd_pct"] and self.peak > 0 and (self.peak - self.cash) / self.peak * 100 >= r["max_dd_pct"]:
            self._lock(r["max_dd_pause_bars"], "max drawdown")
            self.peak = self.cash
        counted = [e for e in self.exits if e[0] > self.lp_from]
        if r["low_profit_trades"] and len(counted) >= r["low_profit_trades"]:
            if sum(e[2] for e in counted[-r["low_profit_trades"]:]) < r["low_profit_min_pct"]:
                self._lock(r["low_profit_pause_bars"], "low profit")
                self.lp_from = self.index

    def _roi_level(self, bar_index: int) -> float | None:
        r, pos = self.risk, self.pos
        if not r["roi_pct"]:
            return None
        pct = r["roi_pct"]
        if r["roi_decay_bars"]:
            pct *= max(0.0, 1 - (bar_index - pos.get("entry_index", bar_index)) / r["roi_decay_bars"])
        return pos["entry_price"] * (1 + pos.get("side", 1) * pct / 100)

    def _move_stop(self, high: float, low: float | None = None) -> None:
        """Trailing / break-even updates after price traded up to `high` (down to `low`)."""
        r, pos = self.risk, self.pos
        d = pos.get("side", 1)
        low = high if low is None else low
        pos["high"] = max(pos.get("high", pos["entry_price"]), high)
        pos["low"] = min(pos.get("low", pos["entry_price"]), low)
        best = pos["high"] if d > 0 else pos["low"]  # most favourable price so far
        one_r = abs(pos["entry_price"] - pos.get("initial_stop", pos["stop"]))
        gain_r = d * (best - pos["entry_price"]) / one_r if one_r > 0 else 0.0
        better = (lambda a, b: a > b) if d > 0 else (lambda a, b: a < b)  # a tighter stop than b?
        if r["breakeven_r"] and gain_r >= r["breakeven_r"]:
            be = pos["entry_price"] * (1 + d * 2 * r["fee_rate"])
            if better(be, pos["stop"]):
                pos["stop"], pos["stop_kind"] = be, "breakeven"
        if r["trail_pct"] and gain_r >= r["trail_activate_r"]:
            trail = best * (1 - d * r["trail_pct"] / 100)
            if better(trail, pos["stop"]):
                pos["stop"], pos["stop_kind"] = trail, "trailing stop"

    def _add(self, price: float) -> None:
        """Scale in at `price` (a resting limit: maker fee, no slippage), within the size caps."""
        r, pos = self.risk, self.pos
        equity = self.cash + pos["qty"] * price
        room = equity * r["max_position_pct"] / 100 / price - pos["qty"]
        qty = min(pos["first_qty"] * r["add_size_pct"] / 100, room, self.cash / (price * (1 + r["maker_fee_rate"])))
        pos["adds"] += 1  # counts even if capped, so a capped add is not retried every minute
        pos["last_fill"] = price
        if qty <= 0:
            return
        fee = qty * price * r["maker_fee_rate"]
        self.cash -= qty * price + fee
        total = pos["qty"] + qty
        pos["entry_price"] = (pos["entry_price"] * pos["qty"] + price * qty) / total
        pos["qty"] = total
        pos["entry_fee"] += fee
        pos["risk_amount"] = max(total * (pos["entry_price"] - pos["initial_stop"]), 0.0)

    def _try_limit(self, bar: Bar, order: dict) -> int:
        """Fill a resting limit buy in this bar if price trades below it. Returns the index of the
        segment (minute) it filled in, so exits are only checked from there on."""
        limit = order["limit"]
        short = order["action"] == "short"
        segments = bar.detail or [(bar.time, bar.open, bar.high, bar.low)]
        for i, (_t, o, h, low) in enumerate(segments):
            if short and h > limit:  # a sell limit above the market fills when price trades through it
                self._open(bar, order, price=max(limit, o), maker=True)
                return i
            if not short and low < limit:
                self._open(bar, order, price=min(limit, o), maker=True)
                return i
        if self.index < order["expires"]:
            self.pending = order  # keep resting
        else:
            self.expired += 1
        return 0

    def _exits(self, bar: Bar, first_seg: int = 0) -> list[dict]:
        """Walk the bar (its 1m path when available) and apply every exit rule in order."""
        r, pos = self.risk, self.pos
        d = pos.get("side", 1)
        segments = (bar.detail or [(bar.time, bar.open, bar.high, bar.low)])[first_seg:]
        target = pos["target"]
        if d < 0:
            return self._exits_short(bar, segments)
        if bar.detail and pos["stop"] is not None and bar.low <= pos["stop"] and target is not None \
                and bar.high >= target:
            self.detail_used += 1
        out: list[dict] = []
        roi = self._roi_level(self.index)
        for _t, o, h, low in segments:
            pos = self.pos
            if pos is None:
                break
            stop = pos["stop"]
            liq = pos.get("liq")
            if liq is not None and low <= liq and (stop is None or liq >= stop):
                out.append(self._close(bar.time, min(liq, o), "liquidation"))
                break
            if stop is not None and low <= stop:
                out.append(self._close(bar.time, min(stop, o), pos.get("stop_kind", "stop")))
                break
            if r["add_max"] and pos.get("adds", 0) < r["add_max"]:
                level = pos["last_fill"] * (1 - r["add_step_pct"] / 100)
                if low <= level and (stop is None or level > stop):
                    self._add(min(level, o))
            if r["partial_tp_r"] and not pos.get("partial_done"):
                level = pos["entry_price"] + (pos["entry_price"] - pos.get("initial_stop", stop)) * r["partial_tp_r"]
                if h >= level:
                    pos["partial_done"] = True
                    out.append(self._close(bar.time, max(level, o), "partial take-profit", r["partial_tp_pct"] / 100))
            levels = [(lv, why) for lv, why in ((target, "target"), (roi, "roi")) if lv is not None]
            if levels:
                level, why = min(levels)
                if h >= level:
                    out.append(self._close(bar.time, max(level, o), why))
                    break
            self._move_stop(h, low)
        return out

    def _exits_short(self, bar: Bar, segments: list) -> list[dict]:
        """The same exit rules as a long, mirrored: the stop is above, targets below."""
        r = self.risk
        out: list[dict] = []
        pos = self.pos
        if bar.detail and pos["stop"] is not None and bar.high >= pos["stop"] and pos["target"] is not None \
                and bar.low <= pos["target"]:
            self.detail_used += 1
        roi = self._roi_level(self.index)
        for _t, o, h, low in segments:
            pos = self.pos
            if pos is None:
                break
            stop, liq, target = pos["stop"], pos.get("liq"), pos["target"]
            if liq is not None and h >= liq and (stop is None or liq <= stop):
                out.append(self._close(bar.time, max(liq, o), "liquidation"))
                break
            if stop is not None and h >= stop:
                out.append(self._close(bar.time, max(stop, o), pos.get("stop_kind", "stop")))
                break
            if r["partial_tp_r"] and not pos.get("partial_done"):
                level = pos["entry_price"] - (pos.get("initial_stop", stop) - pos["entry_price"]) * r["partial_tp_r"]
                if low <= level:
                    pos["partial_done"] = True
                    out.append(self._close(bar.time, min(level, o), "partial take-profit", r["partial_tp_pct"] / 100))
            levels = [(lv, why) for lv, why in ((target, "target"), (roi, "roi")) if lv is not None]
            if levels:
                level, why = max(levels)  # the nearer one for a short is the higher price
                if low <= level:
                    out.append(self._close(bar.time, min(level, o), why))
                    break
            self._move_stop(h, low)
        return out

    def close_all(self, ts: int, price: float, reason: str = "end of test") -> dict | None:
        self.pending = None
        return self._close(ts, price, reason) if self.pos else None


def compute_metrics(trades: list[dict], equity_curve: list[tuple[int, float]], starting_cash: float,
                    bars_in_position: int, total_bars: int, buy_hold_pct: float | None) -> dict:
    final = equity_curve[-1][1] if equity_curve else starting_cash
    peak, max_dd = starting_cash, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    # daily returns for Sharpe (crypto trades every day -> 365)
    daily: dict[str, float] = {}
    for ts, eq in equity_curve:
        daily[_iso(ts)[:10]] = eq
    vals = [starting_cash, *daily.values()]
    rets = [(b / a - 1) for a, b in zip(vals, vals[1:]) if a > 0]
    sharpe = sortino = None
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = mean / sd * math.sqrt(365) if sd > 0 else None
        downside = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / len(rets))  # only losing days count as risk
        sortino = mean / downside * math.sqrt(365) if downside > 0 else None
    calmar = None
    if len(equity_curve) > 1 and max_dd > 0 and final > 0:
        years = max(equity_curve[-1][0] - equity_curve[0][0], 86400) / (365 * 86400)
        calmar = ((final / starting_cash) ** (1 / years) - 1) / max_dd  # annualised return / max drawdown

    n = len(trades)
    return {
        "final_equity": final,
        "net_profit": final - starting_cash,
        "return_pct": (final / starting_cash - 1) * 100,
        "buy_hold_pct": buy_hold_pct,
        "max_drawdown_pct": max_dd * 100,
        "trades": n,
        "win_rate_pct": len(wins) / n * 100 if n else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_r": sum(t["r_multiple"] for t in trades) / n if n else None,
        "expectancy": sum(t["pnl"] for t in trades) / n if n else None,
        "fees": sum(t["fees"] for t in trades),
        "exposure_pct": bars_in_position / total_bars * 100 if total_bars else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
    }
