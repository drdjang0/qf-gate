"""Cascade backtests: replay a ladder over this app's stored candles (ladder.simulate) and save the report."""

from __future__ import annotations

from datetime import datetime

from market.models import Coin
from trading.data import load_bars

from . import ladder
from .models import CascadeBacktest


def run(coin: Coin, kind: str, levels: list[dict], *, interval: str, start: datetime, end: datetime,
        trigger_price: float | None = None, fee_pct: float = 0.2, config: dict | None = None,
        name: str = "", owner=None) -> CascadeBacktest:
    bt = CascadeBacktest(owner=owner, coin=coin, kind=kind, name=name[:100], config=config or {}, trigger_price=trigger_price,
                         fee_pct=fee_pct, interval=interval, start=start, end=end)
    bars = [b for b in load_bars(coin, interval, start, end, detail=False) if b.time >= int(start.timestamp())]
    if not bars:
        bt.status = CascadeBacktest.Status.FAILED
        bt.message = f"No closed {interval} candles for {coin.pair} in this period. Sync the coin first (Coins page)."
        bt.save()
        return bt
    result = ladder.simulate(levels, bars, fee_pct=fee_pct, trigger_price=trigger_price)
    s = result["summary"]
    bt.bars, bt.levels, bt.summary = len(bars), result["levels"], s
    bt.summary.update(first_bar=bars[0].time, last_bar=bars[-1].end)
    bt.roi_pct, bt.realized, bt.fills = s["roi_pct"], s["realized"], s["fills"]
    bt.save()
    return bt
