"""In-memory ExchangeClient for tests (no network)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from market.exchanges.base import CandleData, ChainInfo, CurrencyInfo, ExchangeClient
from market.models import INTERVAL_SECONDS

USDT_ETH = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
PEPE_ETH = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def price_at(ts: int) -> Decimal:
    """Deterministic synthetic price path per UTC day: high 1720 at 00:00, low 1000 at 12:00."""
    minute_of_day = (ts % 86400) // 60
    return Decimal(1000) + Decimal(abs(720 - minute_of_day))


def synth_candle(open_ts: int, step: int) -> CandleData:
    prices = [price_at(t) for t in range(open_ts, open_ts + step, 60)]
    return CandleData(
        open_time=datetime.fromtimestamp(open_ts, tz=timezone.utc),
        open=prices[0], high=max(prices), low=min(prices), close=prices[-1],
        volume_base=Decimal(1), volume_quote=prices[-1], is_closed=True,
    )


class FakeClient(ExchangeClient):
    name = "fake"

    def __init__(self, now: datetime, max_back: int = 10_000):
        self.now = now
        self.max_back = max_back
        self.calls: list[tuple] = []
        self.currencies = [
            CurrencyInfo("BTC", "Bitcoin", False, False, (
                ChainInfo("BTC", "", False, False),
                ChainInfo("BSC", "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c", False, False),
            )),
            CurrencyInfo("USDT", "Tether", False, False, (ChainInfo("ETH", USDT_ETH, False, False),)),
            CurrencyInfo("PEPE", "Pepe", False, False, (ChainInfo("ETH", PEPE_ETH, False, False),)),
            CurrencyInfo("PEPE2", "Fake Pepe", False, False, (
                ChainInfo("BSC", "0x1111111111111111111111111111111111111111", False, False),
            )),
        ]
        self.pairs = {"BTC_USDT": "tradable", "PEPE_USDT": "tradable", "PEPE2_USDT": "untradable"}

    def earliest_available(self, interval, now=None):
        step = INTERVAL_SECONDS[interval]
        n = int((now or self.now).timestamp())
        return datetime.fromtimestamp(n - n % step - (self.max_back - 5) * step, tz=timezone.utc)

    def get_candles(self, pair, interval, start, end):
        self.calls.append(("candles", pair, interval, start, end))
        step = INTERVAL_SECONDS[interval]
        now_ts = int(self.now.timestamp())
        s = int(max(start, self.earliest_available(interval)).timestamp())
        s -= s % step
        e = min(int(end.timestamp()), now_ts)
        out = []
        t = s
        while t <= e:
            c = synth_candle(t, step)
            if t + step > now_ts:
                c = replace(c, is_closed=False)
            out.append(c)
            t += step
        return out

    def get_currency(self, currency):
        return next((c for c in self.currencies if c.currency == currency.upper()), None)

    def list_currencies(self):
        return self.currencies

    def pair_status(self, pair):
        return self.pairs.get(pair.upper())

    # -- live market data ---------------------------------------------------------
    def get_order_book(self, pair, limit=20, group="0"):
        self.calls.append(("book", pair, limit, group))
        asks = [(f"{1001 + i}", "0.5") for i in range(limit)]
        bids = [(f"{1000 - i}", "0.25") for i in range(limit)]
        return {"asks": asks, "bids": bids, "time": int(self.now.timestamp() * 1000)}

    def get_ticker(self, pair):
        return {"last": "1000.5", "change_pct": "-1.5", "high_24h": "1720", "low_24h": "1000",
                "base_volume": "1440", "quote_volume": "1900000", "bid": "1000", "ask": "1001"}

    def get_trades(self, pair, limit=30):
        return [{"time_ms": int(self.now.timestamp() * 1000), "side": "buy", "price": "1000.5", "amount": "0.1"}]

    def pair_info(self, pair):
        return {"price_precision": 1, "amount_precision": 6} if pair.upper() in self.pairs else None
