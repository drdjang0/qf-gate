"""Candle synchronisation: REST (recent), trade archive (older 1m), fallbacks.

Order of sources for each coin over the history window (default 60 days):
1. 1m REST candles for the last ~6.9 days (authoritative, always overwritten
   so a still-forming candle gets its final values).
2. 1m candles rebuilt from Gate's monthly trade archive for everything older
   (exact, tick-level; only for months Gate has published).
3. Any day still without full 1m coverage (typically the start of the current
   month, before its archive exists) is filled with 5m, then 15m, then 1h REST
   candles. The daily high/low *price* stays exact; only the timestamp
   precision drops, and DailyLevel.time_precision records which was used.
4. 1d REST candles, used to cross-check every computed daily high/low.

All writes are idempotent upserts on (coin, interval, open_time).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone

from django.conf import settings
from django.db.models import Count, Max
from django.db.models.functions import TruncDate
from django.utils import timezone as dj_tz

from ..exchanges.base import CandleData, ExchangeClient
from ..exchanges.gate import get_client
from ..models import INTERVAL_SECONDS, Candle, Coin, DailyLevel
from . import archive
from .levels import compute_levels

log = logging.getLogger(__name__)
FALLBACK_INTERVALS = ("5m", "15m", "1h")


def upsert_candles(
    coin: Coin, interval: str, candles: Iterable[CandleData], source: str, overwrite: bool = True
) -> int:
    now = dj_tz.now()
    objs = [
        Candle(
            coin=coin,
            interval=interval,
            open_time=c.open_time,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume_base=c.volume_base,
            volume_quote=c.volume_quote,
            is_closed=c.is_closed,
            buy_volume=c.buy_volume,
            sell_volume=c.sell_volume,
            source=source,
            updated_at=now,
        )
        for c in candles
    ]
    if not objs:
        return 0
    if overwrite:
        fields = ["open", "high", "low", "close", "volume_base", "volume_quote", "is_closed", "source", "updated_at"]
        with_flow = [o for o in objs if o.buy_volume is not None]
        plain = [o for o in objs if o.buy_volume is None]
        # candles without an order-flow split (REST) must not wipe a split stored earlier (archive / stream)
        for group, extra in ((with_flow, ["buy_volume", "sell_volume"]), (plain, [])):
            if group:
                Candle.objects.bulk_create(group, batch_size=2000, update_conflicts=True,
                                           unique_fields=["coin", "interval", "open_time"], update_fields=fields + extra)
    else:
        Candle.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
    return len(objs)


def _utc_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _days(start: datetime, end: datetime) -> list[date]:
    d, out = start.date(), []
    while _utc_midnight(d) < end:
        out.append(d)
        d += timedelta(days=1)
    return out


def coverage_by_day(coin: Coin, interval: str, start: datetime, end: datetime) -> dict[date, int]:
    rows = (
        Candle.objects.filter(coin=coin, interval=interval, open_time__gte=start, open_time__lt=end)
        .annotate(day=TruncDate("open_time", tzinfo=timezone.utc))
        .values("day")
        .annotate(n=Count("id"))
    )
    return {r["day"]: r["n"] for r in rows}


def incomplete_days(coin: Coin, interval: str, start: datetime, end: datetime) -> list[date]:
    """UTC days in [start, end) not fully covered by `interval` candles."""
    step = INTERVAL_SECONDS[interval]
    cov = coverage_by_day(coin, interval, start, end)
    out = []
    for d in _days(start, end):
        lo = max(_utc_midnight(d), start)
        hi = min(_utc_midnight(d) + timedelta(days=1), end)
        expected = int((hi - lo).total_seconds() // step)
        if cov.get(d, 0) < expected:
            out.append(d)
    return out


def _months(start: datetime, end: datetime):
    """(year, month, window_start, window_end) for each calendar month touching [start, end)."""
    y, m = start.year, start.month
    while datetime(y, m, 1, tzinfo=timezone.utc) < end:
        ms = datetime(y, m, 1, tzinfo=timezone.utc)
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        me = datetime(y2, m2, 1, tzinfo=timezone.utc)
        yield y, m, max(ms, start), min(me, end)
        y, m = y2, m2


def sync_coin(
    coin: Coin,
    days: int | None = None,
    client: ExchangeClient | None = None,
    use_archive: bool = True,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> dict:
    client = client or get_client()
    say = progress or (lambda msg: log.info("%s: %s", coin.pair, msg))
    days = days or settings.MARKET["HISTORY_DAYS"]
    now = now or datetime.now(timezone.utc)
    start = _utc_midnight((now - timedelta(days=days)).date())
    pair = coin.pair
    stats = {"rest_1m": 0, "archive_1m": 0, "fallback": {}, "1d": 0}

    # 1) Recent 1m from REST, incremental from the last stored closed candle.
    rest_start = max(start, client.earliest_available("1m", now))
    last_closed = (
        Candle.objects.filter(coin=coin, interval="1m", is_closed=True, open_time__gte=rest_start)
        .aggregate(m=Max("open_time"))["m"]
    )
    fetch_from = last_closed or rest_start
    # earliest moment whose candles may have changed: daily levels and 1d candles are only redone from here
    changed_from = fetch_from
    say(f"1m REST from {fetch_from:%Y-%m-%d %H:%M}Z")
    stats["rest_1m"] = upsert_candles(coin, "1m", client.get_candles(pair, "1m", fetch_from, now), Candle.Source.REST)

    # 2) Older 1m from the trade archive, month by month, only where missing.
    if use_archive and start < rest_start:
        missing = set(incomplete_days(coin, "1m", start, rest_start))
        for y, m, ms, me in _months(start, rest_start):
            if not missing.intersection(_days(ms, me)):
                continue
            say(f"1m from trade archive {y}-{m:02d}")
            candles = archive.archive_candles(pair, y, m, ms, me)
            if candles is None:
                say(f"archive {y}-{m:02d} not published yet; using fallback candles")
                continue
            added = upsert_candles(coin, "1m", candles, Candle.Source.ARCHIVE, overwrite=False)
            stats["archive_1m"] += added
            if added:
                changed_from = min(changed_from, ms)

    # 3) Coarser REST candles for days still lacking full 1m coverage.
    gap_days = incomplete_days(coin, "1m", start, now)
    for interval in FALLBACK_INTERVALS:
        if not gap_days:
            break
        g_start = _utc_midnight(gap_days[0])
        g_end = min(_utc_midnight(gap_days[-1]) + timedelta(days=1), now)
        if g_end <= client.earliest_available(interval, now):
            continue
        say(f"{interval} REST fallback for {len(gap_days)} day(s)")
        n = upsert_candles(coin, interval, client.get_candles(pair, interval, g_start, g_end), Candle.Source.REST)
        stats["fallback"][interval] = n
        changed_from = min(changed_from, g_start)
        still = set(incomplete_days(coin, interval, start, now))
        gap_days = [d for d in gap_days if d in still]

    # 4) Daily candles for cross-checking, and daily levels: only from the first day that changed
    #    (or the whole window if levels are missing, e.g. on the first sync or after a longer history).
    have = DailyLevel.objects.filter(coin=coin, timezone_name=settings.MARKET["DAY_TIMEZONE"],
                                     date__gte=start.date(), date__lt=changed_from.date()).count()
    if have < (changed_from.date() - start.date()).days:
        changed_from = start
    redo = max(start, _utc_midnight(changed_from.date()) - timedelta(days=1))
    stats["1d"] = upsert_candles(coin, "1d", client.get_candles(pair, "1d", redo, now), Candle.Source.REST)
    stats["levels"] = compute_levels(coin, redo.date(), now.date(), now=now)
    return stats


def run_sync(coin_id: int, days: int | None = None, use_archive: bool = True) -> dict:
    """Sync one coin and record status on it (used by the UI background thread and commands)."""
    from django.db import close_old_connections

    close_old_connections()
    coin = Coin.objects.get(pk=coin_id)
    Coin.objects.filter(pk=coin_id).update(sync_status=Coin.SyncStatus.RUNNING, sync_message="Sync started")
    try:
        stats = sync_coin(coin, days=days, use_archive=use_archive)
    except Exception as exc:  # recorded for the UI, then re-raised for logs/commands
        log.exception("Sync failed for %s", coin.pair)
        Coin.objects.filter(pk=coin_id).update(sync_status=Coin.SyncStatus.ERROR, sync_message=str(exc)[:2000])
        raise
    else:
        Coin.objects.filter(pk=coin_id).update(
            sync_status=Coin.SyncStatus.OK, sync_message=str(stats)[:2000], last_synced_at=dj_tz.now()
        )
        return stats
    finally:
        close_old_connections()
