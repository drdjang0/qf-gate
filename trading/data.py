"""Closed bars for strategies, built from the stored 1m candles (same source as the charts).

Each bar above 1m also carries its 1-minute path (`Bar.detail`), so the engine can tell
whether a stop or a target was reached first inside the bar.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Max

from market.models import Candle, Coin
from market.services.chart import CHART_INTERVALS, _bucket, chart_candles

from .engine import Bar


def last_closed_end(coin: Coin) -> int | None:
    """End (unix) of the newest CLOSED 1m candle; bars ending after it are still forming."""
    t = Candle.objects.filter(coin=coin, interval="1m", is_closed=True).aggregate(m=Max("open_time"))["m"]
    return int(t.timestamp()) + 60 if t else None


def load_bars(coin: Coin, interval: str, start: datetime, end: datetime, tz_name: str | None = None,
              detail: bool = True) -> list[Bar]:
    step = CHART_INTERVALS[interval]
    limit = min(int(end.timestamp()), last_closed_end(coin) or int(end.timestamp()))
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    minutes = chart_candles(coin, "1m", start, end, tz_name)
    if step == 60:
        return [Bar(time=c["time"], open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                    volume=c["volume"], step=60, buy_vol=c["buy"], sell_vol=c["sell"])
                for c in minutes if c["time"] + 60 <= limit]
    # aggregate exactly like chart_candles does, keeping each bar's minute path
    tz = ZoneInfo(tz_name)
    bars: dict[int, Bar] = {}
    for c in minutes:
        t = c["time"]
        offset = int(datetime.fromtimestamp(t, tz).utcoffset().total_seconds()) if step >= 14400 else 0
        b = _bucket(t, step, offset)
        bar = bars.get(b)
        if bar is None:
            bar = bars[b] = Bar(time=b, open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                                volume=c["volume"], step=step, detail=[] if detail else None,
                                buy_vol=c["buy"], sell_vol=c["sell"])
        else:
            bar.high = max(bar.high, c["high"])
            bar.low = min(bar.low, c["low"])
            bar.close = c["close"]
            bar.volume += c["volume"]
            if c["buy"] is not None:
                bar.buy_vol = (bar.buy_vol or 0.0) + c["buy"]
                bar.sell_vol = (bar.sell_vol or 0.0) + c["sell"]
        if detail:
            bar.detail.append((t, c["open"], c["high"], c["low"]))
    return [bars[k] for k in sorted(bars) if k + step <= limit]
