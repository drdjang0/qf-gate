from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.models import Candle, Coin, Heartbeat, Job
from market.services import archive, jobs
from market.services.chart import chart_candles, chart_levels
from market.services.levels import compute_levels
from market.services.schedule import Scheduler
from market.services.sync import upsert_candles

from .fakes import synth_candle, utc


def fill(coin, interval, start, end, step):
    t, out = int(start.timestamp()), []
    while t < int(end.timestamp()):
        out.append(synth_candle(t, step))
        t += step
    upsert_candles(coin, interval, out, Candle.Source.REST)


class ChartTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())

    def test_aggregates_1m_into_15m(self):
        d = utc(2026, 10, 10)
        fill(self.coin, "1m", d, d + timedelta(hours=1), 60)
        bars = chart_candles(self.coin, "15m", d, d + timedelta(hours=1), tz_name="UTC")
        self.assertEqual(len(bars), 4)
        self.assertEqual(bars[0]["time"], int(d.timestamp()))
        self.assertEqual(bars[0]["high"], 1720.0)  # 00:00 is the synthetic day high
        self.assertEqual(bars[0]["open"], 1720.0)
        self.assertEqual(bars[0]["close"], 1706.0)  # minute 14 -> 1000 + |720-14|
        self.assertEqual(bars[0]["volume"], 15.0)

    def test_uses_5m_only_where_1m_is_missing(self):
        d = utc(2026, 11, 1)
        fill(self.coin, "5m", d, d + timedelta(minutes=30), 300)
        fill(self.coin, "1m", d + timedelta(minutes=15), d + timedelta(minutes=30), 60)
        bars = chart_candles(self.coin, "5m", d, d + timedelta(minutes=30), tz_name="UTC")
        self.assertEqual(len(bars), 6)
        self.assertEqual([b["volume"] for b in bars], [1.0, 1.0, 1.0, 5.0, 5.0, 5.0])

    def test_daily_bars_follow_day_timezone(self):
        start = utc(2026, 10, 9, 19)  # Asia/Karachi midnight
        fill(self.coin, "1m", start, start + timedelta(days=1), 60)
        bars = chart_candles(self.coin, "1d", start, start + timedelta(days=1), tz_name="Asia/Karachi")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["time"], int(start.timestamp()))

    def test_levels_carry_previous_day(self):
        for i in range(2):
            d = utc(2026, 10, 10 + i)
            fill(self.coin, "1m", d, d + timedelta(days=1), 60)
        compute_levels(self.coin, utc(2026, 10, 10).date(), utc(2026, 10, 11).date(), now=utc(2026, 10, 20))
        lv = chart_levels(self.coin, utc(2026, 10, 11), utc(2026, 10, 12), tz_name="UTC")
        self.assertEqual([x["date"] for x in lv], ["2026-10-11"])
        self.assertEqual((lv[0]["pdh"], lv[0]["pdl"]), (1720.0, 1000.0))
        self.assertEqual(lv[0]["low_time"], int(utc(2026, 10, 11, 12).timestamp()))


class JobQueueTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())

    def test_enqueue_dedupes_active_jobs(self):
        a, created_a = jobs.enqueue(Job.Kind.SYNC, self.coin)
        b, created_b = jobs.enqueue(Job.Kind.SYNC, self.coin)
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(a.pk, b.pk)
        _, created_c = jobs.enqueue(Job.Kind.VERIFY, self.coin)
        self.assertTrue(created_c)

    def test_claim_and_run(self):
        jobs.enqueue(Job.Kind.SYNC, self.coin, {"days": 3})
        job = jobs.claim_next()
        self.assertEqual(job.status, Job.Status.RUNNING)
        self.assertIsNone(jobs.claim_next())
        with mock.patch("market.services.jobs.run_sync", return_value={"rest_1m": 5}) as rs:
            job = jobs.run_job(job)
        rs.assert_called_once_with(self.coin.pk, days=3, use_archive=True)
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertIn("rest_1m", job.message)
        self.assertIsNotNone(job.duration)

    def test_failure_is_recorded(self):
        jobs.enqueue(Job.Kind.SYNC, self.coin)
        job = jobs.claim_next()
        with mock.patch("market.services.jobs.run_sync", side_effect=RuntimeError("Gate down")):
            job = jobs.run_job(job)
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Gate down", job.message)

    def test_recover_interrupted(self):
        jobs.enqueue(Job.Kind.SYNC, self.coin)
        jobs.claim_next()
        self.assertEqual(jobs.recover_interrupted(), 1)
        self.assertEqual(Job.objects.get().status, Job.Status.FAILED)


@override_settings(MARKET={
    **settings.MARKET,
    "SCHEDULE": {"SYNC_EVERY_MINUTES": 15, "DAILY_AT_UTC": "00:05", "KEEP_JOBS_DAYS": 14},
})
class SchedulerTests(TestCase):
    def setUp(self):
        Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        Coin.objects.create(symbol="ETH", chain="ETH", gate_currency="ETH")  # unverified: verify only
        Coin.objects.create(symbol="SOL", chain="SOL", gate_currency="SOL", verified_at=dj_tz.now(), is_active=False)

    def test_periodic_sync_and_daily_verify(self):
        s = Scheduler()
        now = utc(2026, 11, 9, 0, 1)
        created = s.tick(now)
        self.assertEqual([(j.kind, j.coin.symbol) for j in created], [("sync", "BTC")])  # before 00:05: no verify
        self.assertEqual(s.tick(now + timedelta(minutes=3)), [])  # 00:04: sync not due, daily not yet
        Job.objects.update(status=Job.Status.DONE, finished_at=dj_tz.now())

        created = s.tick(now + timedelta(minutes=16))
        kinds = sorted((j.kind, j.coin.symbol) for j in created)
        self.assertEqual(kinds, [("sync", "BTC"), ("verify", "BTC"), ("verify", "ETH")])
        Job.objects.update(status=Job.Status.DONE, finished_at=dj_tz.now())
        later = s.tick(now + timedelta(hours=3))
        self.assertEqual([j.kind for j in later], ["sync"])  # verify only once per day


class ArchiveCacheTests(TestCase):
    def test_unpublished_month_is_not_rechecked_for_an_hour(self):
        archive._NOT_PUBLISHED.clear()
        resp = mock.MagicMock(status_code=404)
        resp.__enter__.return_value = resp
        session = mock.MagicMock()
        session.get.return_value = resp
        self.assertIsNone(archive.fetch_archive("ZZZ_USDT", 2026, 11, session=session))
        self.assertIsNone(archive.fetch_archive("ZZZ_USDT", 2026, 11, session=session))
        self.assertEqual(session.get.call_count, 1)


class Phase2ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())

    def test_candles_json(self):
        url = reverse("market:candles_json", args=[self.coin.pk])
        self.assertEqual(self.client.get(url).status_code, 302)  # login required
        self.client.force_login(self.user)
        now = dj_tz.now().replace(second=0, microsecond=0)
        fill(self.coin, "1m", now - timedelta(hours=2), now + timedelta(minutes=1), 60)
        data = self.client.get(url, {"interval": "1h"}).json()
        self.assertEqual(data["interval"], "1h")
        self.assertGreaterEqual(len(data["candles"]), 2)
        last = data["candles"][-1]["time"]
        inc = self.client.get(url, {"interval": "1h", "since": last}).json()
        self.assertEqual([c["time"] for c in inc["candles"]], [last])
        self.assertEqual(self.client.get(url, {"interval": "7m"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"since": "x"}).status_code, 400)

    def test_sync_button_queues_job_and_warns_without_worker(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("market:coin_sync", args=[self.coin.pk]), {"days": "30"}, follow=True)
        job = Job.objects.get()
        self.assertEqual((job.kind, job.params, job.status), ("sync", {"days": 30}, "queued"))
        self.assertContains(r, "worker is not running")

        Heartbeat.objects.create(name="worker", last_seen=dj_tz.now())
        r = self.client.post(reverse("market:coin_sync", args=[self.coin.pk]), follow=True)
        self.assertEqual(Job.objects.count(), 1)  # deduped
        self.assertContains(r, "already queued")
        self.assertNotContains(r, "worker is not running")

    def test_pages_render_with_status(self):
        self.client.force_login(self.user)
        Heartbeat.objects.create(name="stream", last_seen=dj_tz.now())
        for name in ("market:coin_list", "market:job_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        r = self.client.get(reverse("market:coin_detail", args=[self.coin.pk]))
        self.assertContains(r, 'id="chart"')
        self.assertTrue(r.context["svc_stream_ok"])
        self.assertFalse(r.context["svc_worker_ok"])


class WorkerResilienceTests(TestCase):
    def test_database_error_does_not_kill_the_worker(self):
        from io import StringIO

        from django.core.management import call_command
        from django.db import OperationalError

        real_beat = jobs.beat
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            return real_beat(*a, **kw)

        out = StringIO()
        with mock.patch("market.services.jobs.beat", side_effect=flaky), \
             mock.patch("market.management.commands.run_worker.POLL_SECONDS", 0):
            call_command("run_worker", "--once", "--no-schedule", stdout=out)
        self.assertEqual(len(calls), 2)  # retried after the error
        self.assertIn("retrying", out.getvalue())
        self.assertIn("Worker stopped", out.getvalue())
        self.assertTrue(Heartbeat.objects.filter(name="worker").exists())


class GapReportTests(TestCase):
    def test_statuses_and_repair(self):
        from market.services import gaps

        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        fill(coin, "1m", utc(2026, 10, 1), utc(2026, 10, 2), 60)                       # complete
        fill(coin, "5m", utc(2026, 10, 2), utc(2026, 10, 3), 300)                      # fallback
        fill(coin, "1m", utc(2026, 10, 3), utc(2026, 10, 3, 6), 60)                    # partial
        fill(coin, "1m", utc(2026, 10, 5), utc(2026, 10, 5, 12, 30), 60)               # day 4 missing, today so far
        rep = gaps.coverage(coin, now=utc(2026, 10, 5, 12, 30))
        self.assertEqual([r["status"] for r in rep["days"]], ["complete", "fallback", "partial", "missing", "complete"])
        self.assertEqual(rep["summary"], {"complete": 2, "fallback": 1, "partial": 1, "missing": 1})
        self.assertEqual(rep["first_gap"].isoformat(), "2026-10-03")
        self.assertEqual(gaps.repair_days(rep, today=utc(2026, 10, 5).date()), 3)

        user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("market:coin_detail", args=[coin.pk])), "Data quality")
        self.client.post(reverse("market:coin_repair", args=[coin.pk]))
        self.assertEqual(Job.objects.get().params["days"] > 0, True)
