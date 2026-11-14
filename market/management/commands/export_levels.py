from pathlib import Path

from django.core.management.base import BaseCommand

from market.services.export import build_workbook

from ._util import select_coins


class Command(BaseCommand):
    help = "Export daily high/low (with times and PDH/PDL) to an Excel file."

    def add_arguments(self, parser):
        parser.add_argument("coins", nargs="*")
        parser.add_argument("--out", default="daily-levels.xlsx")
        parser.add_argument("--tz", default=None)

    def handle(self, *args, **opts):
        coins = select_coins(opts["coins"])
        out = Path(opts["out"])
        out.write_bytes(build_workbook(coins, opts["tz"]))
        self.stdout.write(self.style.SUCCESS(f"Wrote {out.resolve()} ({len(coins)} coin(s))"))
