"""Background worker: runs queued jobs and the built-in schedule.

    python manage.py run_worker              # long-running service
    python manage.py run_worker --once       # drain the queue once and exit (for cron / Task Scheduler)
    python manage.py run_worker --no-schedule

Run it next to `stream_candles` (live 1m) and the web server.
"""

from __future__ import annotations

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from market.services import jobs
from market.services.schedule import Scheduler

log = logging.getLogger("market.worker")
POLL_SECONDS = 2
BEAT_SECONDS = 15


class Command(BaseCommand):
    help = "Run queued jobs (sync / verify / levels) and the periodic schedule."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process due + queued jobs, then exit")
        parser.add_argument("--no-schedule", action="store_true", help="Only run jobs queued from the UI")

    def handle(self, *args, **opts):
        self._stop = False
        signal.signal(signal.SIGINT, self._request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._request_stop)

        n = jobs.recover_interrupted()
        if n:
            self.stdout.write(self.style.WARNING(f"Marked {n} interrupted job(s) as failed"))
        scheduler = None if opts["no_schedule"] else Scheduler()
        self.stdout.write(self.style.SUCCESS("Worker started" + ("" if scheduler else " (schedule off)")))

        self._last_beat = 0.0
        while not self._stop:
            close_old_connections()
            try:
                more = self._cycle(scheduler, opts["once"])
            except OperationalError as exc:
                # transient database trouble (SQLite busy, Postgres restart): keep the service alive
                log.warning("Database error in worker loop, retrying: %s", exc)
                self.stdout.write(self.style.WARNING(f"Database busy/unavailable, retrying: {exc}"))
                close_old_connections()
                time.sleep(POLL_SECONDS)
                continue
            if not more:
                break
        self.stdout.write("Worker stopped.")

    def _cycle(self, scheduler, once: bool) -> bool:
        """One pass: heartbeat, schedule, at most one job. Returns False when --once has drained the queue."""
        now = time.monotonic()
        if now - self._last_beat >= BEAT_SECONDS:
            jobs.beat("worker", "schedule on" if scheduler else "schedule off")
            self._last_beat = now
        if scheduler:
            for job in scheduler.tick():
                self.stdout.write(f"Scheduled: {job}")
            scheduler.run_periodic()
        job = jobs.claim_next()
        if job is None:
            if once:
                return False
            time.sleep(POLL_SECONDS)
            return True
        self.stdout.write(f"Running job {job.pk}: {job}")
        job = jobs.run_job(job)
        style = self.style.SUCCESS if job.status == job.Status.DONE else self.style.ERROR
        self.stdout.write(style(f"  -> {job.status} in {job.duration}: {job.message[:300]}"))
        return True

    def _request_stop(self, *_):
        self.stdout.write("Stopping after the current job...")
        self._stop = True
