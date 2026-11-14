"""Exchange-agnostic types and interface.

Everything above this layer (sync, levels, future strategies and bots) talks to
`ExchangeClient`, never to Gate.io directly. Adding another exchange, or the
order-placement methods needed for automated trading (phase 4), means adding
an adapter here without touching the data pipeline.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CandleData:
    open_time: datetime  # tz-aware UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    volume_quote: Decimal
    is_closed: bool
    buy_volume: Decimal | None = None  # taker-buy base volume (order flow), when known
    sell_volume: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ChainInfo:
    chain: str
    contract_address: str  # "" for a native coin on its own chain
    deposit_disabled: bool
    withdraw_disabled: bool


@dataclass(frozen=True, slots=True)
class CurrencyInfo:
    currency: str  # exchange's own code, e.g. "BTC"
    name: str
    delisted: bool
    trade_disabled: bool
    chains: tuple[ChainInfo, ...]


class ExchangeError(RuntimeError):
    """Raised for exchange/API errors (bad params, rate limits after retries...)."""


class ExchangeClient(abc.ABC):
    name: str

    @abc.abstractmethod
    def get_candles(self, pair: str, interval: str, start: datetime, end: datetime) -> list[CandleData]:
        """Candles with start <= open_time <= end, oldest first."""

    @abc.abstractmethod
    def earliest_available(self, interval: str, now: datetime | None = None) -> datetime:
        """Oldest candle open time the API will still serve for this interval."""

    @abc.abstractmethod
    def get_currency(self, currency: str) -> CurrencyInfo | None:
        """Currency metadata incl. per-chain contract addresses; None if unknown."""

    @abc.abstractmethod
    def list_currencies(self) -> list[CurrencyInfo]:
        """All currencies listed on the exchange."""

    @abc.abstractmethod
    def pair_status(self, pair: str) -> str | None:
        """Trading status of a spot pair (e.g. 'tradable'); None if it does not exist."""

    # -- public market data (live page). Optional: adapters that lack it raise NotImplementedError.
    def get_order_book(self, pair: str, limit: int = 20, group: str = "0") -> dict:
        """{'asks': [(price, amount), ...] lowest first, 'bids': [...] highest first, 'time': ms}.
        Prices/amounts are decimal strings. `group` merges price levels (e.g. "10"); "0" = none."""
        raise NotImplementedError

    def get_ticker(self, pair: str) -> dict | None:
        """24h ticker: last, change_pct, high_24h, low_24h, base_volume, quote_volume, bid, ask."""
        raise NotImplementedError

    def get_trades(self, pair: str, limit: int = 30) -> list[dict]:
        """Latest public trades, newest first: {'time_ms', 'side', 'price', 'amount'}."""
        raise NotImplementedError

    def pair_info(self, pair: str) -> dict | None:
        """{'price_precision': int, 'amount_precision': int} for a spot pair; None if unknown."""
        raise NotImplementedError
