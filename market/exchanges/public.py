"""Read-only public market data from several exchanges, for the Scanner (no keys, nothing stored).

Small direct adapters instead of a large dependency: each one knows its exchange's candle and 24h
ticker endpoints and maps them to CandleData / a common ticker dict. Gate.io stays the platform's
exchange of record (identity, stored history, live feed, trading); these are for research only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests

from .base import CandleData, ExchangeError

INTERVALS = ("15m", "30m", "1h", "4h", "1d")
SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
TIMEOUT = 15
UA = {"User-Agent": "qf-gate/1.0", "Accept": "application/json"}


def _get(url: str, params: dict, session=None):
    http = session or requests
    try:
        r = http.get(url, params=params, timeout=TIMEOUT, headers=UA)
    except requests.RequestException as exc:
        raise ExchangeError(f"unreachable ({exc.__class__.__name__})") from exc
    if r.status_code in (400, 404):
        return None
    if not r.ok:
        raise ExchangeError(f"HTTP {r.status_code}")
    return r.json()


@dataclass(frozen=True)
class Exchange:
    key: str
    name: str
    symbol: Callable[[str, str], str]
    fetch: Callable  # (symbol, interval, start_s, end_s, session) -> rows [ms, open, high, low, close, base_vol, quote_vol]
    tick: Callable  # (symbol, session) -> {"last", "change_pct", "quote_volume"} | None
    page: int  # max candles per request


# -- adapters ---------------------------------------------------------------------------------------


def _gate_fetch(sym, iv, s, e, session):
    rows = _get("https://api.gateio.ws/api/v4/spot/candlesticks",
                {"currency_pair": sym, "interval": iv, "from": s, "to": e}, session)
    if rows is None:
        return None
    return [[int(r[0]) * 1000, r[5], r[3], r[4], r[2], r[6], r[1]] for r in rows]


def _gate_tick(sym, session):
    rows = _get("https://api.gateio.ws/api/v4/spot/tickers", {"currency_pair": sym}, session)
    if not rows:
        return None
    t = rows[0]
    return {"last": t["last"], "change_pct": float(t.get("change_percentage") or 0), "quote_volume": t.get("quote_volume")}


def _binance_like(base_url, iv_map, pct_is_fraction):
    def fetch(sym, iv, s, e, session):
        rows = _get(f"{base_url}/klines", {"symbol": sym, "interval": iv_map[iv], "startTime": s * 1000,
                                           "endTime": e * 1000, "limit": 1000}, session)
        if rows is None:
            return None
        return [[int(r[0]), r[1], r[2], r[3], r[4], r[5], r[7]] for r in rows]

    def tick(sym, session):
        t = _get(f"{base_url}/ticker/24hr", {"symbol": sym}, session)
        if not t or "lastPrice" not in t:
            return None
        pct = float(t.get("priceChangePercent") or 0)
        return {"last": t["lastPrice"], "change_pct": pct * 100 if pct_is_fraction else pct,
                "quote_volume": t.get("quoteVolume")}
    return fetch, tick


_BINANCE = _binance_like("https://api.binance.com/api/v3", {i: i for i in INTERVALS}, False)
_MEXC = _binance_like("https://api.mexc.com/api/v3", {"15m": "15m", "30m": "30m", "1h": "60m", "4h": "4h", "1d": "1d"}, True)


def _bybit_fetch(sym, iv, s, e, session):
    d = _get("https://api.bybit.com/v5/market/kline", {"category": "spot", "symbol": sym, "limit": 1000, "start": s * 1000,
             "end": e * 1000, "interval": {"15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}[iv]}, session)
    if not d or d.get("retCode") != 0:
        return None
    return [[int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6]] for r in d["result"]["list"]]


def _bybit_tick(sym, session):
    d = _get("https://api.bybit.com/v5/market/tickers", {"category": "spot", "symbol": sym}, session)
    if not d or d.get("retCode") != 0 or not d["result"]["list"]:
        return None
    t = d["result"]["list"][0]
    return {"last": t["lastPrice"], "change_pct": float(t.get("price24hPcnt") or 0) * 100, "quote_volume": t.get("turnover24h")}


def _kucoin_fetch(sym, iv, s, e, session):
    d = _get("https://api.kucoin.com/api/v1/market/candles", {"symbol": sym, "startAt": s, "endAt": e,
             "type": {"15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day"}[iv]}, session)
    if not d or d.get("code") != "200000":
        return None
    # KuCoin order: time, open, CLOSE, HIGH, LOW, volume, turnover
    return [[int(r[0]) * 1000, r[1], r[3], r[4], r[2], r[5], r[6]] for r in d["data"]]


def _kucoin_tick(sym, session):
    d = _get("https://api.kucoin.com/api/v1/market/stats", {"symbol": sym}, session)
    if not d or d.get("code") != "200000" or not d["data"].get("last"):
        return None
    t = d["data"]
    return {"last": t["last"], "change_pct": float(t.get("changeRate") or 0) * 100, "quote_volume": t.get("volValue")}


def _bitget_fetch(sym, iv, s, e, session):
    d = _get("https://api.bitget.com/api/v2/spot/market/candles", {"symbol": sym, "startTime": s * 1000,
             "endTime": e * 1000, "limit": 1000,
             "granularity": {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}[iv]}, session)
    if not d or d.get("code") != "00000":
        return None
    return [[int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6]] for r in d["data"]]


def _bitget_tick(sym, session):
    d = _get("https://api.bitget.com/api/v2/spot/market/tickers", {"symbol": sym}, session)
    if not d or d.get("code") != "00000" or not d["data"]:
        return None
    t = d["data"][0]
    o, last = float(t.get("open") or 0), float(t["lastPr"])
    return {"last": t["lastPr"], "change_pct": (last / o - 1) * 100 if o else 0.0, "quote_volume": t.get("quoteVolume")}


def _okx_fetch(sym, iv, s, e, session):
    d = _get("https://www.okx.com/api/v5/market/history-candles", {"instId": sym, "limit": 100, "after": (e + 1) * 1000,
             "bar": {"15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}[iv]}, session)
    if not d or d.get("code") != "0":
        return None
    return [[int(r[0]), r[1], r[2], r[3], r[4], r[5], r[7]] for r in d["data"] if int(r[0]) >= s * 1000]


def _okx_tick(sym, session):
    d = _get("https://www.okx.com/api/v5/market/ticker", {"instId": sym}, session)
    if not d or d.get("code") != "0" or not d["data"]:
        return None
    t = d["data"][0]
    o, last = float(t.get("open24h") or 0), float(t["last"])
    return {"last": t["last"], "change_pct": (last / o - 1) * 100 if o else 0.0, "quote_volume": t.get("volCcy24h")}


EXCHANGES: dict[str, Exchange] = {x.key: x for x in (
    Exchange("gate", "Gate.io", lambda b, q: f"{b}_{q}", _gate_fetch, _gate_tick, 1000),
    Exchange("binance", "Binance", lambda b, q: f"{b}{q}", _BINANCE[0], _BINANCE[1], 1000),
    Exchange("okx", "OKX", lambda b, q: f"{b}-{q}", _okx_fetch, _okx_tick, 100),
    Exchange("bybit", "Bybit", lambda b, q: f"{b}{q}", _bybit_fetch, _bybit_tick, 1000),
    Exchange("kucoin", "KuCoin", lambda b, q: f"{b}-{q}", _kucoin_fetch, _kucoin_tick, 1500),
    Exchange("mexc", "MEXC", lambda b, q: f"{b}{q}", _MEXC[0], _MEXC[1], 1000),
    Exchange("bitget", "Bitget", lambda b, q: f"{b}{q}", _bitget_fetch, _bitget_tick, 1000),
)}


def _row(r, step: int, now_ms: float) -> CandleData:
    return CandleData(open_time=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                      open=Decimal(str(r[1])), high=Decimal(str(r[2])), low=Decimal(str(r[3])), close=Decimal(str(r[4])),
                      volume_base=Decimal(str(r[5] or 0)), volume_quote=Decimal(str(r[6] or 0)),
                      is_closed=r[0] + step * 1000 <= now_ms)


def candles(exchange: str, base: str, quote: str, interval: str, start: datetime, end: datetime,
            session=None) -> list[CandleData] | None:
    """Candles with start <= open < end, oldest first; None if the exchange does not list the pair."""
    ex = EXCHANGES[exchange]
    sym = ex.symbol(base.upper(), quote.upper())
    step = SECONDS[interval]
    s, e = int(start.timestamp()), int(end.timestamp())
    out: dict[int, list] = {}
    cursor = s
    while cursor < e:
        chunk_end = min(e, cursor + step * (ex.page - 1))
        rows = ex.fetch(sym, interval, cursor, chunk_end, session)
        if rows is None:
            if not out:
                return None
            break
        for r in rows:
            if s * 1000 <= r[0] < e * 1000:
                out[r[0]] = r
        cursor = chunk_end + step
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return [_row(r, step, now_ms) for _t, r in sorted(out.items())]


def ticker(exchange: str, base: str, quote: str, session=None) -> dict | None:
    ex = EXCHANGES[exchange]
    return ex.tick(ex.symbol(base.upper(), quote.upper()), session)


# -- all tickers at once (Screener) -----------------------------------------------------------------
# One request per exchange returns every spot pair's 24h stats. Each adapter maps them to:
# {"base", "last", "bid", "ask", "high", "low", "change_pct", "quote_volume"} (floats or None).


def _fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _split(sym: str, quote: str, sep: str = "") -> str | None:
    """Base of `sym` if it is quoted in `quote`, else None (BTCUSDT / BTC-USDT / BTC_USDT)."""
    suffix = f"{sep}{quote}"
    if sym.upper().endswith(suffix) and len(sym) > len(suffix):
        return sym[: -len(suffix)].upper()
    return None


def _tk(base, last, bid, ask, high, low, change_pct, quote_volume) -> dict:
    return {"base": base, "last": _fl(last), "bid": _fl(bid), "ask": _fl(ask), "high": _fl(high), "low": _fl(low),
            "change_pct": _fl(change_pct) or 0.0, "quote_volume": _fl(quote_volume) or 0.0}


def _pct_from_open(last, open_):
    last, open_ = _fl(last), _fl(open_)
    return (last / open_ - 1) * 100 if last and open_ else 0.0


def _gate_all(quote, session):
    rows = _get("https://api.gateio.ws/api/v4/spot/tickers", {}, session) or []
    return [_tk(b, t.get("last"), t.get("highest_bid"), t.get("lowest_ask"), t.get("high_24h"), t.get("low_24h"),
                t.get("change_percentage"), t.get("quote_volume"))
            for t in rows if (b := _split(t.get("currency_pair", ""), quote, "_"))]


def _binance_like_all(base_url, pct_is_fraction):
    def all_(quote, session):
        rows = _get(f"{base_url}/ticker/24hr", {}, session) or []
        k = 100 if pct_is_fraction else 1
        return [_tk(b, t.get("lastPrice"), t.get("bidPrice"), t.get("askPrice"), t.get("highPrice"), t.get("lowPrice"),
                    (_fl(t.get("priceChangePercent")) or 0) * k, t.get("quoteVolume"))
                for t in rows if (b := _split(t.get("symbol", ""), quote))]
    return all_


def _bybit_all(quote, session):
    d = _get("https://api.bybit.com/v5/market/tickers", {"category": "spot"}, session)
    if not d or d.get("retCode") != 0:
        return []
    return [_tk(b, t.get("lastPrice"), t.get("bid1Price"), t.get("ask1Price"), t.get("highPrice24h"), t.get("lowPrice24h"),
                (_fl(t.get("price24hPcnt")) or 0) * 100, t.get("turnover24h"))
            for t in d["result"]["list"] if (b := _split(t.get("symbol", ""), quote))]


def _kucoin_all(quote, session):
    d = _get("https://api.kucoin.com/api/v1/market/allTickers", {}, session)
    if not d or d.get("code") != "200000":
        return []
    return [_tk(b, t.get("last"), t.get("buy"), t.get("sell"), t.get("high"), t.get("low"),
                (_fl(t.get("changeRate")) or 0) * 100, t.get("volValue"))
            for t in d["data"]["ticker"] if (b := _split(t.get("symbol", ""), quote, "-"))]


def _bitget_all(quote, session):
    d = _get("https://api.bitget.com/api/v2/spot/market/tickers", {}, session)
    if not d or d.get("code") != "00000":
        return []
    return [_tk(b, t.get("lastPr"), t.get("bidPr"), t.get("askPr"), t.get("high24h"), t.get("low24h"),
                _pct_from_open(t.get("lastPr"), t.get("open")), t.get("quoteVolume"))
            for t in d["data"] if (b := _split(t.get("symbol", ""), quote))]


def _okx_all(quote, session):
    d = _get("https://www.okx.com/api/v5/market/tickers", {"instType": "SPOT"}, session)
    if not d or d.get("code") != "0":
        return []
    return [_tk(b, t.get("last"), t.get("bidPx"), t.get("askPx"), t.get("high24h"), t.get("low24h"),
                _pct_from_open(t.get("last"), t.get("open24h")), t.get("volCcy24h"))
            for t in d["data"] if (b := _split(t.get("instId", ""), quote, "-"))]


ALL_TICKERS: dict[str, Callable] = {
    "gate": _gate_all, "binance": _binance_like_all("https://api.binance.com/api/v3", False),
    "okx": _okx_all, "bybit": _bybit_all, "kucoin": _kucoin_all,
    "mexc": _binance_like_all("https://api.mexc.com/api/v3", True), "bitget": _bitget_all,
}


def all_tickers(exchange: str, quote: str, session=None) -> list[dict]:
    """Every `quote` spot pair's 24h stats on one exchange (one request). Raises ExchangeError if unreachable."""
    return ALL_TICKERS[exchange](quote.upper(), session)
