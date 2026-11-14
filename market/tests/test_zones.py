from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.models import Candle, Coin
from market.services import zones as Z
from market.services.levels import compute_levels
from market.services.sync import upsert_candles

from .fakes import synth_candle, utc


def day(d, o, h, low, c, prev=None, complete=True):
    return Z.Day(date(2026, 10, d), o, h, low, c, complete, prev)


class ZoneRuleTests(TestCase):
    def test_forward_only_and_crossing_counts(self):
        days = [
            day(1, 95, 100, 90, 95),            # forms High 100 -> zone 99..101
            day(2, 95, 99.5, 94, 98, prev=95),  # high reaches the zone, closes below: held
            day(3, 98, 105, 97, 104, prev=98),  # trades straight through and closes above: broke
            day(4, 104, 106, 102, 103, prev=104),  # stays above, never in the zone: no touch
        ]
        zs = Z.build_zones(days, [("high", date(2026, 10, 1), 100.0, None),
                                  ("high", date(2026, 10, 3), 105.0, None)], 0.01)
        first, later = zs
        self.assertEqual([(t.date.day, t.result) for t in first.touches], [(2, "held"), (3, "broke")])
        self.assertEqual((first.held, first.broke), (1, 1))
        # day 2 (before it formed) traded nowhere near 105, and day 4 (after) reached 106 -> 1 touch
        self.assertEqual([t.date.day for t in later.touches], [4])

    def test_results(self):
        lo, up = 99.0, 101.0
        self.assertEqual(Z.touch_result(95, day(2, 95, 100, 94, 96), lo, up), "held")    # from below, rejected
        self.assertEqual(Z.touch_result(95, day(2, 95, 103, 94, 102), lo, up), "broke")  # from below, closed above
        self.assertEqual(Z.touch_result(105, day(2, 105, 106, 100, 104), lo, up), "held")  # from above, held
        self.assertEqual(Z.touch_result(105, day(2, 105, 106, 97, 98), lo, up), "broke")   # from above, closed below
        self.assertEqual(Z.touch_result(100, day(2, 100, 102, 98, 99), lo, up), "inside")
        self.assertEqual(Z.touch_result(95, day(2, 95, 103, 94, 102, complete=False), lo, up), "live")
        self.assertEqual(Z.touch_result(None, day(2, 95, 100, 94, 96), lo, up), "held")  # no prev day: uses open

    def test_first_entry_minute(self):
        t0 = datetime(2026, 10, 2, tzinfo=timezone.utc)
        bars = [(t0 + timedelta(minutes=i), 90 + i, 91 + i, 89 + i) for i in range(20)]  # rising 1/min
        m = Z.Minutes.build(bars)
        self.assertEqual(m.first_entry(99, 101), t0 + timedelta(minutes=8))  # high 99 first at minute 8
        self.assertEqual(m.first_entry(85, 90.5), t0)  # open already inside
        self.assertIsNone(m.first_entry(200, 210))
        falling = Z.Minutes.build([(t0 + timedelta(minutes=i), 120 - i, 121 - i, 119 - i) for i in range(20)])
        self.assertEqual(falling.first_entry(99, 101), t0 + timedelta(minutes=18))  # low 101 first at minute 18

    def test_hotspots_count_distinct_days_and_never_overlap(self):
        days = [day(i, 100, 101, 99, 100, prev=100 if i > 1 else None) for i in range(1, 6)]
        levels = [("high", d.date, 101.0, None) for d in days] + [("low", d.date, 99.0, None) for d in days]
        zs = Z.build_zones(days, levels, 0.01)
        self.assertEqual(sum(len(z.touches) for z in zs), 20)  # 10 zones, each touched by every later day
        hs = Z.hotspots(days, zs, 0.01)
        self.assertEqual(hs[0]["touches"], 4)  # 4 later days, not 20 zone-touches
        for i, a in enumerate(hs):
            for b in hs[i + 1:]:
                self.assertFalse(a["lower"] <= b["upper"] and b["lower"] <= a["upper"])
        hm = Z.heatmap(days, zs, 0.01)
        self.assertEqual(sum(c["n"] for row in hm["rows"] for c in row["cells"]), 20)


class ZoneAnalysisTests(TestCase):
    def setUp(self):
        cache.clear()
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        t0 = int(utc(2026, 10, 1).timestamp())
        upsert_candles(self.coin, "1m", [synth_candle(t, 60) for t in range(t0, t0 + 3 * 86400, 60)],
                       Candle.Source.REST)
        compute_levels(self.coin, date(2026, 10, 1), date(2026, 10, 3), tz_name="UTC")

    def test_analyse_with_touch_minutes(self):
        r = Z.analyse(self.coin, 1.0, "UTC")
        self.assertEqual((len(r["days"]), len(r["zones"])), (3, 6))
        high1 = next(z for z in r["zones"] if z.kind == "high" and z.date == date(2026, 10, 1))
        low1 = next(z for z in r["zones"] if z.kind == "low" and z.date == date(2026, 10, 1))
        # synthetic day: starts at 1720 (the high), falls to 1000 at 12:00
        self.assertEqual([t.time.strftime("%d %H:%M") for t in high1.touches], ["02 00:00", "03 00:00"])
        self.assertEqual([t.time.strftime("%d %H:%M") for t in low1.touches], ["02 11:50", "03 11:50"])
        self.assertEqual(len(r["hotspots"]), 2)
        self.assertEqual(Z.analyse(self.coin, 1.0, "UTC")["zones"][0].price, r["zones"][0].price)  # cached

    def test_page_and_export(self):
        user = User.objects.create_user("me", password="not-a-real-pass-123")
        url = reverse("market:zones_page", args=[self.coin.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(user)
        with self.settings(MARKET={**__import__("django.conf").conf.settings.MARKET, "DAY_TIMEZONE": "UTC"}):
            page = self.client.get(url, {"width": "1", "kind": "low", "min": "1"})
            self.assertContains(page, "Hotspots")
            self.assertContains(page, "Heatmap")
            self.assertContains(page, "11:50")
            self.assertNotContains(page, ">High</td>")  # filtered to low zones
            self.assertEqual(self.client.get(url, {"width": "abc"}).status_code, 200)  # falls back to default
            x = self.client.get(reverse("market:zones_export", args=[self.coin.pk]), {"width": "99"})
            self.assertEqual(x.status_code, 200)
            self.assertIn("5.0pct", x["Content-Disposition"])  # width clamped to the maximum
            self.assertTrue(x.content.startswith(b"PK"))


class PriceFilterTests(TestCase):
    def test_fprice(self):
        from market.templatetags.market_extras import fprice

        self.assertEqual(fprice(65815.3), "65,815.30")
        self.assertEqual(fprice(0.000012345), "0.000012345")
        self.assertEqual(fprice(1.5), "1.5000")
        self.assertEqual(fprice(None), "—")
