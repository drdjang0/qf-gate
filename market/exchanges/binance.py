"""Binance spot candles, read-only, for comparison with Gate.io (public API, no key).

Binance publishes no contract addresses, so a coin is matched by ticker only. `services/compare.py`
therefore checks the prices agree before the comparison is trusted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import requests

from .base import CandleData, ExchangeError

BASE = "https://api.binance.com/api/v3"
INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


class BinanceClient:
    name = "binance"

    def __init__(self, session=None, timeout: float = 15):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: dict):
        try:
            r = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ExchangeError(f"Binance unreachable: {exc}") from exc
        if r.status_code == 400:
            return None  # unknown symbol
        if not r.ok:
            raise ExchangeError(f"Binance {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def get_candles(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[CandleData] | None:
        """Closed and forming candles in [start, end]; None if Binance does not list the symbol."""
        out, cursor = [], int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        while cursor <= end_ms:
            rows = self._get("/klines", {"symbol": symbol, "interval": INTERVALS[interval], "startTime": cursor,
                                         "endTime": end_ms, "limit": 1000})
            if rows is None:
                return None
            if not rows:
                break
            for r in rows:
                vol, taker_buy = Decimal(r[5]), Decimal(r[9])
                out.append(CandleData(
                    open_time=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                    open=Decimal(r[1]), high=Decimal(r[2]), low=Decimal(r[3]), close=Decimal(r[4]),
                    volume_base=vol, volume_quote=Decimal(r[7]), is_closed=r[6] < now_ms,
                    buy_volume=taker_buy, sell_volume=vol - taker_buy,
                ))
            cursor = rows[-1][0] + 1
            if len(rows) < 1000:
                break
        return out
