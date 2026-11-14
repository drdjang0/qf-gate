"""Exact 1m candles from Gate.io's public monthly trade archive.

Gate.io's REST API only serves the latest 10,000 one-minute candles (~6.9
days). For older 1m history we rebuild candles from every individual trade in
Gate's official archive:

    {ARCHIVE_BASE}/spot/deals/{YYYYMM}/{PAIR}-{YYYYMM}.csv.gz

Each line: unix_ts_with_micros, trade_id, price, amount, side
e.g.      1790812800.805660,221240701,83616.5,0.011959,2

A month's file is only published after the month ends, so the current month
is covered by the REST API (last ~6.9 days) plus coarser REST candles for any
remaining gap (see services/sync.py).
"""

from __future__ import annotations

import gzip
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from django.conf import settings

from ..exchanges.base import CandleData, ExchangeError

log = logging.getLogger(__name__)


def archive_url(pair: str, year: int, month: int) -> str:
    ym = f"{year:04d}{month:02d}"
    return f"{settings.GATE['ARCHIVE_BASE'].rstrip('/')}/spot/deals/{ym}/{pair.upper()}-{ym}.csv.gz"


_NOT_PUBLISHED: dict[tuple[str, int, int], float] = {}
NOT_PUBLISHED_RECHECK_SECONDS = 3600


def fetch_archive(pair: str, year: int, month: int, session=None) -> Path | None:
    """Download (or reuse the cached copy of) a monthly trade file. None if not published."""
    cache_dir: Path = settings.MARKET["ARCHIVE_CACHE_DIR"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{pair.upper()}-{year:04d}{month:02d}.csv.gz"
    if path.exists() and path.stat().st_size > 0:
        return path
    key = (pair.upper(), year, month)
    if time.monotonic() - _NOT_PUBLISHED.get(key, -1e9) < NOT_PUBLISHED_RECHECK_SECONDS:
        return None  # checked recently; Gate publishes a month's file only after it ends
    url = archive_url(pair, year, month)
    session = session or requests
    log.info("Downloading trade archive %s", url)
    try:
        with session.get(url, stream=True, timeout=(15, 120)) as resp:
            if resp.status_code in (403, 404):
                _NOT_PUBLISHED[key] = time.monotonic()
                return None
            if not resp.ok:
                raise ExchangeError(f"Archive {url} -> HTTP {resp.status_code}")
            tmp = path.with_suffix(".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            tmp.replace(path)
    except requests.RequestException as exc:
        raise ExchangeError(f"Archive download failed: {exc}") from exc
    return path


@dataclass
class _Bucket:
    open_key: tuple
    close_key: tuple
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    vol_base: Decimal
    vol_quote: Decimal
    buy: Decimal = Decimal(0)
    sell: Decimal = Decimal(0)


def candles_from_trades(lines, start: datetime, end: datetime) -> list[CandleData]:
    """Aggregate trade lines into 1m candles for start <= minute < end.

    Minutes without trades between the first and last traded minute get a
    flat zero-volume candle at the previous close, the same way exchange
    candle feeds represent quiet minutes.
    """
    start_ts, end_ts = start.timestamp(), end.timestamp()
    buckets: dict[int, _Bucket] = {}
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "ignore")
        parts = raw.strip().split(",")
        if len(parts) < 4:
            continue
        try:
            ts = float(parts[0])
            trade_id = int(parts[1])
            price = Decimal(parts[2])
            amount = Decimal(parts[3])
        except (ValueError, ArithmeticError):
            continue
        if ts < start_ts or ts >= end_ts:
            continue
        minute = int(ts // 60) * 60
        key = (ts, trade_id)
        is_buy = len(parts) > 4 and parts[4].strip() == "1"  # Gate archive: 1 = taker buy, 2 = taker sell
        b = buckets.get(minute)
        if b is None:
            buckets[minute] = _Bucket(key, key, price, price, price, price, amount, price * amount,
                                      amount if is_buy else Decimal(0), Decimal(0) if is_buy else amount)
            continue
        if is_buy:
            b.buy += amount
        else:
            b.sell += amount
        if key < b.open_key:
            b.open_key, b.open = key, price
        if key > b.close_key:
            b.close_key, b.close = key, price
        if price > b.high:
            b.high = price
        if price < b.low:
            b.low = price
        b.vol_base += amount
        b.vol_quote += price * amount

    out: list[CandleData] = []
    prev: CandleData | None = None
    for m in sorted(buckets):
        if prev is not None:
            gap = int(prev.open_time.timestamp()) + 60
            while gap < m:
                out.append(
                    CandleData(
                        open_time=datetime.fromtimestamp(gap, tz=timezone.utc),
                        open=prev.close, high=prev.close, low=prev.close, close=prev.close,
                        volume_base=Decimal(0), volume_quote=Decimal(0), is_closed=True,
                        buy_volume=Decimal(0), sell_volume=Decimal(0),
                    )
                )
                gap += 60
        b = buckets[m]
        prev = CandleData(
            open_time=datetime.fromtimestamp(m, tz=timezone.utc),
            open=b.open, high=b.high, low=b.low, close=b.close,
            volume_base=b.vol_base, volume_quote=b.vol_quote, is_closed=True,
            buy_volume=b.buy, sell_volume=b.sell,
        )
        out.append(prev)
    return out


def archive_candles(pair: str, year: int, month: int, start: datetime, end: datetime) -> list[CandleData] | None:
    """1m candles for [start, end) within one month; None if the month is not published yet."""
    path = fetch_archive(pair, year, month)
    if path is None:
        return None
    with gzip.open(path, "rb") as fh:
        return candles_from_trades(fh, start, end)
