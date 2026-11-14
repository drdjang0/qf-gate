from django.conf import settings
from django.core.management.base import BaseCommand

from market.services.sync import run_sync

from ._util import select_coins


class Command(BaseCommand):
    help = "Fetch candle history from Gate.io (REST + trade archive) and recompute daily levels."

    def add_arguments(self, parser):
        parser.add_argument("coins", nargs="*", help="id / symbol / pair / contract (default: all active)")
        parser.add_argument("--days", type=int, default=settings.MARKET["HISTORY_DAYS"])
        parser.add_argument("--no-archive", action="store_true", help="Skip the monthly trade archive download")

    def handle(self, *args, **opts):
        failed = 0
        for coin in select_coins(opts["coins"]):
            if not coin.verified_at:
                self.stderr.write(self.style.WARNING(f"Skipping {coin}: not verified (run verify_coins)"))
                continue
            self.stdout.write(f"Syncing {coin.pair} ({opts['days']} days)...")
            try:
                stats = run_sync(coin.pk, days=opts["days"], use_archive=not opts["no_archive"])
            except Exception as exc:  # noqa: BLE001 - keep going with other coins
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {coin.pair} failed: {exc}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"  {coin.pair}: {stats}"))
        if failed:
            raise SystemExit(1)
