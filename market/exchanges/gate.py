"""Gate.io v4 public REST adapter (market data only; no API keys needed).

Verified Gate.io limits (Nov 2026):
- /spot/candlesticks returns at most 1000 candles per request.
- It only serves the most recent 10,000 candles of an interval. For 1m that
  is ~6.9 days, for 5m ~34.7 days, 15m ~104 days. Older 1m history therefore
  comes from Gate's monthly trade archive (see services/archive.py).
- Candle row: [t, quote_volume, close, high, low, open, base_volume, is_closed]
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import requests
from django.conf import settings

from ..models import INTERVAL_SECONDS
from .base import CandleData, ChainInfo, CurrencyInfo, ExchangeClient, ExchangeError

log = logging.getLogger(__name__)

MAX_POINTS_PER_REQUEST = 1000
MAX_POINTS_BACK = 10_000
# Stay a little inside the 10,000-point window so a request built a few
# seconds before it is sent is not rejected as "too long ago".
SAFETY_POINTS = 5
CURRENCY_CACHE_SECONDS = 3600
# Gate answers "doesn't exist" with HTTP 400 + one of these labels instead of 404.
NOT_FOUND_LABELS = {"INVALID_CURRENCY_PAIR", "INVALID_CURRENCY", "CURRENCY_NOT_FOUND"}


def _label(resp) -> str:
    try:
        return str(resp.json().get("label", ""))
    except ValueError:
        return ""


def _dt(ts: int | str) -> datetime:
    return datetime.fromtimestamp(int(float(ts)), tz=timezone.utc)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def parse_rest_candle(row: list) -> CandleData:
    return CandleData(
        open_time=_dt(row[0]),
        volume_quote=Decimal(row[1]),
        close=Decimal(row[2]),
        high=Decimal(row[3]),
        low=Decimal(row[4]),
        open=Decimal(row[5]),
        volume_base=Decimal(row[6]) if len(row) > 6 else Decimal(0),
        is_closed=(str(row[7]).lower() == "true") if len(row) > 7 else True,
    )


def parse_ws_candle(result: dict) -> tuple[str, str, CandleData]:
    """Parse a spot.candlesticks WebSocket update -> (interval, pair, candle)."""
    interval, pair = result["n"].split("_", 1)
    candle = CandleData(
        open_time=_dt(result["t"]),
        open=Decimal(result["o"]),
        high=Decimal(result["h"]),
        low=Decimal(result["l"]),
        close=Decimal(result["c"]),
        volume_base=Decimal(result.get("a", "0")),
        volume_quote=Decimal(result.get("v", "0")),
        is_closed=bool(result.get("w", False)),
    )
    return interval, pair, candle


def _parse_currency(data: dict) -> CurrencyInfo:
    chains = tuple(
        ChainInfo(
            chain=str(c.get("name", "")).upper(),
            contract_address=str(c.get("addr") or ""),
            deposit_disabled=bool(c.get("deposit_disabled")),
            withdraw_disabled=bool(c.get("withdraw_disabled")),
        )
        for c in data.get("chains") or []
    )
    return CurrencyInfo(
        currency=str(data["currency"]).upper(),
        name=str(data.get("name", "")),
        delisted=bool(data.get("delisted")),
        trade_disabled=bool(data.get("trade_disabled")),
        chains=chains,
    )


class GateClient(ExchangeClient):
    name = "gate"

    def __init__(self, base_url: str | None = None, timeout: float | None = None, session=None):
        cfg = settings.GATE
        self.base_url = (base_url or cfg["REST_BASE"]).rstrip("/")
        self.timeout = timeout or cfg["TIMEOUT"]
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "qf-gate/1.0"})
        self._currency_cache: tuple[float, list[CurrencyInfo]] | None = None
        self._lock = threading.Lock()

    # -- HTTP -----------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None, retries: int = 4):
        url = f"{self.base_url}{path}"
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise ExchangeError(f"GET {path} failed: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    raise ExchangeError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
                wait = float(resp.headers.get("Retry-After") or 2**attempt)
                log.warning("Gate %s -> %s, retrying in %.0fs", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 400 and _label(resp) in NOT_FOUND_LABELS:
                return None
            if not resp.ok:
                raise ExchangeError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise ExchangeError(f"GET {path}: retries exhausted")  # pragma: no cover

    # -- candles --------------------------------------------------------------
    def earliest_available(self, interval: str, now: datetime | None = None) -> datetime:
        step = INTERVAL_SECONDS[interval]
        now_ts = _ts(now or datetime.now(timezone.utc))
        current_open = now_ts - now_ts % step
        return _dt(current_open - (MAX_POINTS_BACK - SAFETY_POINTS) * step)

    def get_candles(self, pair: str, interval: str, start: datetime, end: datetime) -> list[CandleData]:
        step = INTERVAL_SECONDS[interval]
        start_ts = _ts(max(start, self.earliest_available(interval)))
        start_ts -= start_ts % step
        end_ts = _ts(end)
        out: dict[datetime, CandleData] = {}
        cursor = start_ts
        while cursor <= end_ts:
            chunk_end = min(cursor + (MAX_POINTS_PER_REQUEST - 1) * step, end_ts)
            rows = self._get(
                "/spot/candlesticks",
                {"currency_pair": pair, "interval": interval, "from": cursor, "to": chunk_end},
            )
            if rows is None:
                raise ExchangeError(f"Unknown pair {pair}")
            for row in rows:
                c = parse_rest_candle(row)
                out[c.open_time] = c
            cursor = chunk_end + step
            if cursor <= end_ts:
                time.sleep(0.05)  # stay well under the public rate limit
        return [out[k] for k in sorted(out)]

    # -- metadata -------------------------------------------------------------
    def get_currency(self, currency: str) -> CurrencyInfo | None:
        data = self._get(f"/spot/currencies/{currency.upper()}")
        return _parse_currency(data) if data else None

    def list_currencies(self) -> list[CurrencyInfo]:
        with self._lock:
            if self._currency_cache and time.monotonic() - self._currency_cache[0] < CURRENCY_CACHE_SECONDS:
                return self._currency_cache[1]
            data = self._get("/spot/currencies") or []
            parsed = [_parse_currency(d) for d in data if d.get("currency")]
            self._currency_cache = (time.monotonic(), parsed)
            return parsed

    def pair_status(self, pair: str) -> str | None:
        data = self._get(f"/spot/currency_pairs/{pair.upper()}")
        return str(data.get("trade_status", "")) if data else None


    # -- live market data (no retries with long back-off: stale data is worse than none) --
    def get_order_book(self, pair: str, limit: int = 20, group: str = "0") -> dict:
        data = self._get("/spot/order_book",
                         {"currency_pair": pair, "limit": limit, "interval": group or "0"}, retries=1)
        if data is None:
            raise ExchangeError(f"Unknown pair {pair}")
        return {
            "asks": [(p, a) for p, a in data.get("asks", [])],
            "bids": [(p, a) for p, a in data.get("bids", [])],
            "time": int(data.get("current") or 0),
        }

    def get_ticker(self, pair: str) -> dict | None:
        data = self._get("/spot/tickers", {"currency_pair": pair}, retries=1)
        if not data:
            return None
        t = data[0]
        return {
            "last": t.get("last"), "change_pct": t.get("change_percentage"),
            "high_24h": t.get("high_24h"), "low_24h": t.get("low_24h"),
            "base_volume": t.get("base_volume"), "quote_volume": t.get("quote_volume"),
            "bid": t.get("highest_bid"), "ask": t.get("lowest_ask"),
        }

    def get_all_tickers(self) -> list[dict]:
        """24h tickers of every spot pair (one request)."""
        return self._get("/spot/tickers", retries=1) or []

    def get_trades(self, pair: str, limit: int = 30) -> list[dict]:
        data = self._get("/spot/trades", {"currency_pair": pair, "limit": limit}, retries=1) or []
        return [
            {"time_ms": int(float(t.get("create_time_ms") or 0)), "side": t.get("side"),
             "price": t.get("price"), "amount": t.get("amount")}
            for t in data
        ]

    def pair_info(self, pair: str) -> dict | None:
        data = self._get(f"/spot/currency_pairs/{pair.upper()}")
        if not data:
            return None
        def num(key):
            v = data.get(key)
            return float(v) if v not in (None, "") else None

        return {"price_precision": int(data.get("precision") or 0),
                "amount_precision": int(data.get("amount_precision") or 0),
                # Gate: "null means no limit"
                "min_base_amount": num("min_base_amount"), "min_quote_amount": num("min_quote_amount"),
                "trade_status": data.get("trade_status") or ""}


_default_client: GateClient | None = None


def get_client() -> GateClient:
    """Shared client (keeps the HTTP connection pool and currency cache warm)."""
    global _default_client
    if _default_client is None:
        _default_client = GateClient()
    return _default_client
