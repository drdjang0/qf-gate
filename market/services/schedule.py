"""Built-in scheduler: decides when periodic jobs are due and queues them.

- Every `SYNC_EVERY_MINUTES`: an incremental sync of every active, verified
  coin. Cheap (REST 1m from the last stored candle) and a safety net for the
  live stream. It also upgrades fallback (5m) days to exact 1m as soon as Gate
  publishes the month's trade archive.
- Once a day at `DAILY_AT_UTC`: re-verify every coin's identity on Gate.io
  (catches delistings / contract migrations) and prune old job records.
"""

from __future__ import annotations

import logging
import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from django.conf import settings

from ..models import Coin, Job
from . import jobs


log = logging.getLogger(__name__)

# Light, frequent tasks other apps run inside the worker loop (not recorded as Jobs).
_PERIODIC: dict[str, tuple[int, Callable[[], object]]] = {}


def register_periodic(name: str, every_seconds: int, func: Callable[[], object]) -> None:
    _PERIODIC[name] = (every_seconds, func)


@dataclass
class Scheduler:
    last_sync: datetime | None = None
    last_daily: date | None = None
    last_periodic: dict[str, float] = field(default_factory=dict)

    def run_periodic(self, now: float | None = None) -> list[str]:
        """Run registered periodic tasks that are due (monotonic clock). Errors are logged, not raised."""
        now = time_mod.monotonic() if now is None else now
        ran = []
        for name, (every, func) in _PERIODIC.items():
            if now - self.last_periodic.get(name, -1e18) < every:
                continue
            self.last_periodic[name] = now
            try:
                func()
            except Exception:  # noqa: BLE001
                log.exception("Periodic task %s failed", name)
            ran.append(name)
        return ran

    def tick(self, now: datetime | None = None) -> list[Job]:
        """Queue whatever is due at `now`; returns the newly created jobs."""
        now = now or datetime.now(timezone.utc)
        cfg = settings.MARKET["SCHEDULE"]
        created: list[Job] = []

        minutes = cfg["SYNC_EVERY_MINUTES"]
        if minutes > 0 and (self.last_sync is None or now - self.last_sync >= timedelta(minutes=minutes)):
            self.last_sync = now
            for coin in Coin.objects.filter(is_active=True, verified_at__isnull=False):
                job, new = jobs.enqueue(
                    Job.Kind.SYNC, coin, {"days": settings.MARKET["HISTORY_DAYS"]}, origin=Job.Origin.SCHEDULE
                )
                if new:
                    created.append(job)

        hh, mm = (int(x) for x in cfg["DAILY_AT_UTC"].split(":"))
        daily_due = datetime.combine(now.date(), time(hh, mm), tzinfo=timezone.utc)
        if now >= daily_due and self.last_daily != now.date():
            self.last_daily = now.date()
            for coin in Coin.objects.filter(is_active=True):
                job, new = jobs.enqueue(Job.Kind.VERIFY, coin, origin=Job.Origin.SCHEDULE)
                if new:
                    created.append(job)
            jobs.prune(cfg["KEEP_JOBS_DAYS"])
        return created
