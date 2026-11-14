from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.exchanges.base import ExchangeError
from market.models import Candle, Coin
from market.services import live
from market.services.history import daily_history, summarize
from market.services.levels import compute_levels
from market.services.sync import upsert_candles

from .fakes import FakeClient, synth_candle, utc


class LiveSnapshotTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_ = FakeClient(now=utc(2026, 11, 9, 12))

    def test_group_options(self):
        self.assertEqual(live.group_options(1), ["0.1", "1", "10", "100"])
        self.assertEqual(live.group_options(0), ["1", "10", "100", "1000"])
        self.assertEqual(live.group_options(8)[0], "0.00000001")

    def test_snapshot_totals_spread_and_cache(self):
        d = live.snapshot(self.client_, "BTC_USDT", depth=10)
        self.assertEqual(len(d["asks"]), 10)
        self.assertEqual([r["total"] for r in d["asks"][:3]], ["0.5", "1", "1.5"])  # cumulative from best price
        self.assertEqual(d["bids"][0]["price"], "1000")
        self.assertEqual(d["spread"]["abs"], "1")
        self.assertEqual(d["ticker"]["last"], "1000.5")
        live.snapshot(self.client_, "BTC_USDT", depth=10)
        self.assertEqual(sum(1 for c in self.client_.calls if c[0] == "book"), 1)  # served from the 1 s cache
        live.snapshot(self.client_, "BTC_USDT", depth=999)  # unsupported depth -> default
        self.assertEqual(self.client_.calls[-1][2], live.DEFAULT_DEPTH)


class DailyHistoryTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        t0 = int(utc(2026, 10, 1).timestamp())
        candles = []
        for t in range(t0, t0 + 3 * 86400, 60):
            c = synth_candle(t, 60)
            if t0 + 2 * 86400 + 3600 == t:  # day 3: one spike above the previous day's high
                c = replace(c, high=c.high + 500)
            candles.append(c)
        upsert_candles(self.coin, "1m", candles, Candle.Source.REST)
        compute_levels(self.coin, utc(2026, 10, 1).date(), utc(2026, 10, 3).date(), tz_name="UTC")

    def test_rows(self):
        rows = daily_history(self.coin, "UTC")
        self.assertEqual([r["level"].date.isoformat() for r in rows], ["2026-10-03", "2026-10-02", "2026-10-01"])
        newest, middle, oldest = rows
        self.assertEqual(oldest["volume"], 1440)  # one unit per 1m candle
        self.assertIsNone(oldest["change_pct"])
        self.assertIsNone(oldest["pdh"])
        self.assertAlmostEqual(middle["change_pct"], 0.0)
        self.assertFalse(middle["broke_pdh"])  # equal high is not a break
        self.assertTrue(newest["broke_pdh"])
        self.assertFalse(newest["broke_pdl"])
        self.assertAlmostEqual(oldest["range_pct"], 72.0)  # 1000 -> 1720
        self.assertEqual(newest["start"], int(utc(2026, 10, 3).timestamp()))

        s = summarize(rows)
        self.assertEqual((s["days"], s["with_prev"], s["broke_pdh"], s["broke_pdl"]), (3, 2, 1, 0))
        self.assertEqual(s["high"].date.isoformat(), "2026-10-03")
        self.assertIsNone(summarize([]))


class MarketPageViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.fake = FakeClient(now=dj_tz.now())
        patcher = mock.patch("market.views.get_client", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_login_required(self):
        self.assertEqual(self.client.get(reverse("market:market_page", args=[self.coin.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse("market:book_json", args=[self.coin.pk])).status_code, 302)

    def test_page_renders(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("market:market_page_default"))
        self.assertRedirects(r, reverse("market:market_page", args=[self.coin.pk]))
        now = dj_tz.now().replace(second=0, microsecond=0)
        t = int((now - timedelta(hours=3)).timestamp())
        upsert_candles(self.coin, "1m", [synth_candle(t + i * 60, 60) for i in range(120)], Candle.Source.REST)
        compute_levels(self.coin, (now - timedelta(days=1)).date(), now.date(), tz_name="UTC")
        page = self.client.get(reverse("market:market_page", args=[self.coin.pk]))
        self.assertContains(page, "Order book")
        self.assertContains(page, "day-by-day history")
        self.assertContains(page, 'class="day"')
        self.assertContains(page, '<option value="10">10</option>')  # grouping from pair precision 1

    def test_book_json(self):
        self.client.force_login(self.user)
        url = reverse("market:book_json", args=[self.coin.pk])
        d = self.client.get(url, {"depth": "10", "group": "1"}).json()
        self.assertEqual((len(d["asks"]), len(d["bids"])), (10, 10))
        self.assertEqual(self.fake.calls[-1], ("book", "BTC_USDT", 10, "1"))
        self.assertEqual(self.client.get(url, {"group": "7"}).status_code, 400)
        cache.clear()
        with mock.patch.object(self.fake, "get_order_book", side_effect=ExchangeError("down")):
            r = self.client.get(url)
        self.assertEqual(r.status_code, 502)
        self.assertIn("down", r.json()["error"])
