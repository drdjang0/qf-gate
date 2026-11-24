"""Pure cascade math: level pricing and sizing, skip-ahead selection and the backtest replay. No I/O.

Ported to Python from the user's own gate-cascade-bot (src/cascadeMath.js), keeping its rules:

- one buy level rests at a time; the next is only considered once the previous one's BUY has filled
  (its take-profit sell runs on its own track and never gates the next level);
- skip-ahead: a level whose buy price is at or above the market would fill at once instead of waiting
  for the dip, so it is skipped and the next deeper level tried, except the last level, which is placed
  anyway so that tranche is not left undeployed;
- each filled level gets its own take-profit sell at fill price x (1 + take_profit_pct / 100);
- fees follow Gate.io's convention: deducted from what is received on both legs.
"""

from __future__ import annotations

from typing import Any


def compute_levels(base_price: float, base_amount: float, price_step_pct: float, amount_step_pct: float,
                   num_levels: int, tp_base_pct: float, tp_step_pct: float) -> list[dict]:
    """% based cascade. Price compounds down by price_step_pct per level, the USDT amount compounds up by
    amount_step_pct, and the take-profit % ladders up additively: base + (i - 1) x step."""
    if not base_price > 0:
        raise ValueError("Level 1 buy price must be > 0")
    if not base_amount > 0:
        raise ValueError("Level 1 amount must be > 0")
    if not (isinstance(num_levels, int) and num_levels >= 1):
        raise ValueError("Number of levels must be a positive whole number")
    if not 0 <= price_step_pct < 100:
        raise ValueError("Price step must be between 0 and 100 %")
    if amount_step_pct <= -100:
        raise ValueError("Amount step must be above -100 %")
    levels = []
    for i in range(1, num_levels + 1):
        tp = tp_base_pct + (i - 1) * tp_step_pct
        if tp <= 0:
            raise ValueError(f"Level {i}: take-profit must be above 0 % (base {tp_base_pct} + step {tp_step_pct})")
        levels.append({
            "index": i,
            "target_price": base_price * (1 - price_step_pct / 100) ** (i - 1),
            "amount_usdt": base_amount * (1 + amount_step_pct / 100) ** (i - 1),
            "take_profit_pct": tp,
        })
    return levels


def sell_target(fill_price: float, take_profit_pct: float) -> float:
    if not fill_price > 0:
        raise ValueError("fill price must be > 0")
    return fill_price * (1 + take_profit_pct / 100)


def normalize_manual(rows: list[dict]) -> list[dict]:
    """Manual template: each level's exact buy price, USDT amount and sell price -> the same level shape
    as compute_levels, so the engine and the backtester treat both kinds the same way."""
    if not rows:
        raise ValueError("At least one level is required")
    out = []
    for i, r in enumerate(rows, start=1):
        try:
            buy, amount, sell = float(r["target_price"]), float(r["amount_usdt"]), float(r["sell_price"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Level {i}: buy price, amount and sell price must all be numbers") from None
        if not (buy > 0 and amount > 0 and sell > 0):
            raise ValueError(f"Level {i}: buy price, amount and sell price must all be positive")
        if sell <= buy:
            raise ValueError(f"Level {i}: sell price ({sell:g}) must be higher than the buy price ({buy:g})")
        out.append({"index": i, "target_price": buy, "amount_usdt": amount, "take_profit_pct": (sell / buy - 1) * 100})
    return out


def parse_manual_text(text: str) -> list[dict]:
    """'buy, amount, sell' per line (commas, semicolons, tabs or spaces) -> rows for normalize_manual."""
    rows = []
    for n, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in line.replace(";", ",").replace("\t", ",").replace(" ", ",").split(",") if p]
        if len(parts) != 3:
            raise ValueError(f"Line {n}: expected 3 numbers (buy price, USDT amount, sell price), got {len(parts)}")
        rows.append({"target_price": parts[0], "amount_usdt": parts[1], "sell_price": parts[2]})
    return rows


def select_next(pending: list[dict], market_price: float) -> tuple[dict | None, list[dict]]:
    """Which pending level (sorted by index) to place now, and which to skip because the market is already
    at or below their buy price. The last remaining level is placed even if it would fill at once."""
    skip = []
    for i, lvl in enumerate(pending):
        if lvl["target_price"] >= market_price and i < len(pending) - 1:
            skip.append(lvl)
            continue
        return lvl, skip
    return None, skip


def _f(bar: Any, name: str) -> float:
    return float(bar[name] if isinstance(bar, dict) else getattr(bar, name))


def simulate(levels: list[dict], bars: list, fee_pct: float = 0.2, trigger_price: float | None = None) -> dict:
    """Replay a cascade over closed bars (oldest first; objects or dicts with time/open/high/low/close).

    Decision prices never use a later value than the live engine would have had: the first bar's open
    (no trigger) or the trigger price itself on the first bar whose low reaches it; after a level's buy
    fills, that bar's low. A resting buy fills on the first bar from its placement whose low reaches the
    buy price, recorded at the buy price. Its sell fills on the first bar AFTER the buy bar whose high
    reaches the sell price (OHLC can't prove the order inside one bar), and the next level's search also
    starts on the next bar. Sells still open at the end are valued at the last close (unrealised).
    """
    res = [{**lv, "status": "pending", "sell_status": "none", "fill_price": None, "fill_time": None,
            "qty": None, "sell_price": None, "sell_time": None, "pnl": None, "skip_reason": "", "skip_price": None}
           for lv in levels]
    empty = {"armed": False, "armed_at": None, "armed_price": None, "invested": 0.0, "realized": 0.0,
             "unrealized": 0.0, "roi_pct": 0.0, "roi_total_pct": 0.0, "fills": 0, "skips": 0, "sells": 0,
             "open_qty": 0.0, "end_price": None, "buy_hold_pct": None}
    if not bars:
        return {"levels": res, "summary": {**empty, "outcome": "no_data"}}
    fee = fee_pct / 100
    first_open, last_close = _f(bars[0], "open"), _f(bars[-1], "close")
    empty.update(end_price=last_close, buy_hold_pct=(last_close / first_open - 1) * 100 if first_open else None)

    if trigger_price is None:
        arm_i, decision = 0, first_open
    else:
        arm_i = next((i for i, b in enumerate(bars) if _f(b, "low") <= trigger_price), None)
        if arm_i is None:
            return {"levels": res, "summary": {**empty, "outcome": "never_armed"}}
        decision = trigger_price
    armed_at = int(_f(bars[arm_i], "time"))

    search, invested, realized, unrealized, fills, skips, sells, open_qty = arm_i, 0.0, 0.0, 0.0, 0, 0, 0, 0.0
    while True:
        pending = [lv for lv in res if lv["status"] == "pending"]
        if not pending:
            break
        place, skip = select_next(pending, decision)
        for lv in skip:
            lv.update(status="skipped", skip_reason="market already below the buy price", skip_price=decision)
            skips += 1
        if place is None:
            break
        buy_i = next((i for i in range(search, len(bars)) if _f(bars[i], "low") <= place["target_price"]), None)
        if buy_i is None:
            place["status"] = "placed"  # still resting when the data ends
            break
        qty = place["amount_usdt"] / place["target_price"] * (1 - fee)
        place.update(status="filled", fill_price=place["target_price"], fill_time=int(_f(bars[buy_i], "time")), qty=qty)
        fills += 1
        invested += place["amount_usdt"]
        target = sell_target(place["target_price"], place["take_profit_pct"])
        sell_i = next((i for i in range(buy_i + 1, len(bars)) if _f(bars[i], "high") >= target), None)
        if sell_i is None:
            place["sell_status"] = "placed"
            place["sell_price"] = target
            unrealized += qty * last_close * (1 - fee) - place["amount_usdt"]
            open_qty += qty
        else:
            pnl = qty * target * (1 - fee) - place["amount_usdt"]
            place.update(sell_status="filled", sell_price=target, sell_time=int(_f(bars[sell_i], "time")), pnl=pnl)
            realized += pnl
            sells += 1
        decision = _f(bars[buy_i], "low")
        search = buy_i + 1

    return {"levels": res, "summary": {
        **empty, "outcome": "completed", "armed": True, "armed_at": armed_at,
        "armed_price": trigger_price if trigger_price is not None else first_open,
        "invested": invested, "realized": realized, "unrealized": unrealized,
        "roi_pct": realized / invested * 100 if invested else 0.0,
        "roi_total_pct": (realized + unrealized) / invested * 100 if invested else 0.0,
        "fills": fills, "skips": skips, "sells": sells, "open_qty": open_qty,
    }}
