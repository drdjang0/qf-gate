"""Add the order-flow split (taker buy / sell volume) to stored 1m candles from cached trade archives.

    python manage.py backfill_orderflow [coin ...]

Reads only the monthly archive files already in MARKET_ARCHIVE_CACHE_DIR (no downloads) and rewrites
those months' 1m candles from the trades, now including buy and sell volume.
"""

from __future__ import annotations

import calendar
import gzip
import re
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from market.models import Candle
from market.services.archive import candles_from_trades
from market.services.sync import upsert_candles

from ._util import select_coins

FILE = re.compile(r"^(?P<pair>[A-Z0-9]+_[A-Z0-9]+)-(?P<y>\d{4})(?P<m>\d{2})\.csv\.gz$")


class Command(BaseCommand):
    help = "Backfill taker buy/sell volume on 1m candles from cached Gate.io trade archives."

    def add_arguments(self, parser):
        parser.add_argument("coins", nargs="*")

    def handle(self, *args, **opts):
        cache = settings.MARKET["ARCHIVE_CACHE_DIR"]
        for coin in select_coins(opts["coins"]):
            files = sorted(p for p in cache.glob(f"{coin.pair}-*.csv.gz") if FILE.match(p.name))
            if not files:
                self.stdout.write(f"{coin.pair}: no cached archives")
                continue
            for path in files:
                m = FILE.match(path.name)
                y, mo = int(m["y"]), int(m["m"])
                start = datetime(y, mo, 1, tzinfo=timezone.utc)
                end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc)
                with gzip.open(path, "rb") as fh:
                    candles = candles_from_trades(fh, start, end)
                n = upsert_candles(coin, "1m", candles, Candle.Source.ARCHIVE)
                self.stdout.write(f"{coin.pair} {y}-{mo:02d}: {n} candles with order flow "
                                  f"({calendar.month_abbr[mo]} {y})")
