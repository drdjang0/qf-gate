from django.core.management.base import BaseCommand

from market.exchanges.gate import get_client
from market.services.verify import verify_coin

from ._util import select_coins


class Command(BaseCommand):
    help = "Check each coin's chain + contract address against Gate.io and resolve its Gate currency code."

    def add_arguments(self, parser):
        parser.add_argument("coins", nargs="*")

    def handle(self, *args, **opts):
        client = get_client()
        bad = 0
        for coin in select_coins(opts["coins"], active_only=False):
            res = verify_coin(client, coin)
            style = self.style.SUCCESS if res.ok else self.style.ERROR
            bad += not res.ok
            self.stdout.write(style(f"{coin} -> {coin.pair}: {res.summary}"))
        if bad:
            raise SystemExit(1)
