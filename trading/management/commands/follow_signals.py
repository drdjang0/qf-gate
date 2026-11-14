"""Follow another QF-gate instance: poll its signal API and hand new signals to 'Follow remote signals' bots.

    python manage.py follow_signals --url https://other.example/ --name other [--token-env OTHER_SIGNAL_TOKEN]

The producer's token is read from the environment variable named by --token-env (default
REMOTE_SIGNAL_TOKEN), never from the command line.
"""

from __future__ import annotations

import os
import time

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Max

from trading import signals_share
from trading.models import LiveSignal


class Command(BaseCommand):
    help = "Poll another instance's signals and execute them with 'Follow remote signals' bots."

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="Base URL of the producing instance")
        parser.add_argument("--name", required=True, help="Short name to store its signals under")
        parser.add_argument("--token-env", default="REMOTE_SIGNAL_TOKEN")
        parser.add_argument("--every", type=int, default=10, help="Seconds between polls")
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **o):
        token = os.environ.get(o["token_env"], "")
        if not token:
            raise CommandError(f"Set the producer's token in the {o['token_env']} environment variable.")
        if o["name"] == "local":
            raise CommandError("'local' is reserved for this instance's own signals.")
        url = o["url"].rstrip("/") + "/trading/api/v1/signals"
        self.stdout.write(self.style.SUCCESS(f"Following {o['name']} at {url}"))
        while True:
            close_old_connections()
            since = LiveSignal.objects.filter(source=o["name"]).aggregate(m=Max("remote_id"))["m"] or 0
            try:
                r = requests.get(url, params={"since": since}, headers={"Authorization": f"Bearer {token}"}, timeout=15)
                r.raise_for_status()
                new = signals_share.ingest(o["name"], r.json().get("signals", []))
                sent = signals_share.dispatch()
                if new:
                    self.stdout.write(f"{len(new)} new signal(s), {sent} order(s)")
            except (requests.RequestException, ValueError) as exc:
                self.stderr.write(f"Poll failed: {exc}")
            if o["once"]:
                break
            time.sleep(o["every"])
