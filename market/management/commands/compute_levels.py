from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from market.services.levels import compute_levels

from ._util import select_coins


class Command(BaseCommand):
    help = "Recompute daily high/low from stored candles (e.g. after changing MARKET_DAY_TIMEZONE)."

    def add_arguments(self, parser):
        parser.add_argument("coins", nargs="*")
        parser.add_argument("--days", type=int, default=settings.MARKET["HISTORY_DAYS"])
        parser.add_argument("--tz", default=None, help="IANA timezone for the day boundary (default: setting)")

    def handle(self, *args, **opts):
        # one day of slack on each side covers any timezone offset; future days are skipped
        end = datetime.now(timezone.utc).date() + timedelta(days=1)
        start = end - timedelta(days=opts["days"] + 1)
        for coin in select_coins(opts["coins"]):
            n = compute_levels(coin, start, end, tz_name=opts["tz"])
            self.stdout.write(f"{coin.pair}: {n} day(s)")
