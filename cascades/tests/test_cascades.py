"""Cascades: level math, backtest replay, the dry-run engine and the pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db.models import F
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from cascades import engine, ladder
from cascades.models import Cascade, CascadeBacktest, CascadeLevel
from market.models import Candle, Coin
from trading.models import TradingControl

L, S = CascadeLevel.Status, CascadeLevel.SellStatus
T = datetime(2026, 11, 1, 12, 0, 30, tzinfo=timezone.utc)  # mid-minute on purpose
FOUR = dict(base_price=100, base_amount=10, price_step_pct=10, amount_step_pct=0, num_levels=4, tp_base_pct=5, tp_step_pct=1)


def levels4():
    return ladder.compute_levels(*FOUR.values())


def bar(t, o, h, lo, c):
    return {"time": t, "open": o, "high": h, "low": lo, "close": c}


class LadderMathTests(TestCase):
    def test_compute_levels(self):
        lv = ladder.compute_levels(100, 10, 10, 50, 3, 5, 1)
        self.assertEqual([round(x["target_price"], 6) for x in lv], [100, 90, 81])
        self.assertEqual([round(x["amount_usdt"], 6) for x in lv], [10, 15, 22.5])
        self.assertEqual([x["take_profit_pct"] for x in lv], [5, 6, 7])
        self.assertEqual([x["index"] for x in lv], [1, 2, 3])

    def test_compute_levels_validation(self):
        for args in [(0, 10, 1, 0, 3, 5, 1), (100, 0, 1, 0, 3, 5, 1), (100, 10, 1, 0, 0, 5, 1), (100, 10, 100, 0, 3, 5, 1),
                     (100, 10, 1, 0, 3, -5, 1)]:
            with self.assertRaises(ValueError):
                ladder.compute_levels(*args)

    def test_manual_template(self):
        rows = ladder.parse_manual_text("# comment\n0.0722, 10, 0.0758\n0.0710;12;0.0752\n\n0.07 15 0.075")
        lv = ladder.normalize_manual(rows)
        self.assertEqual(len(lv), 3)
        self.assertAlmostEqual(lv[0]["take_profit_pct"], (0.0758 / 0.0722 - 1) * 100)
        self.assertEqual(lv[2]["amount_usdt"], 15)
        with self.assertRaisesRegex(ValueError, "higher than the buy"):
            ladder.normalize_manual([{"target_price": 1, "amount_usdt": 1, "sell_price": 1}])
        with self.assertRaisesRegex(ValueError, "expected 3 numbers"):
            ladder.parse_manual_text("1, 2")
        with self.assertRaises(ValueError):
            ladder.normalize_manual([])

    def test_select_next_skip_ahead_and_last_level(self):
        pending = levels4()
        place, skip = ladder.select_next(pending, 105)
        self.assertEqual((place["index"], skip), (1, []))
        place, skip = ladder.select_next(pending, 85)  # 100 and 90 are already above the market
        self.assertEqual((place["index"], [s["index"] for s in skip]), (3, [1, 2]))
        place, skip = ladder.select_next(pending, 50)  # all above: the last is placed anyway
        self.assertEqual((place["index"], len(skip)), (4, 3))
        self.assertEqual(ladder.select_next([], 1), (None, []))


class SimulateTests(TestCase):
    def test_fast_crash_skip_ahead(self):
        bars = [bar(0, 105, 106, 99, 100), bar(60, 100, 101, 70, 75), bar(120, 75, 96, 72, 95), bar(180, 95, 106, 94, 100)]
        r = ladder.simulate(levels4(), bars, fee_pct=0)
        lv, s = r["levels"], r["summary"]
        self.assertEqual([x["status"] for x in lv], ["filled", "filled", "skipped", "filled"])
        self.assertEqual([x["fill_time"] for x in lv], [0, 60, None, 120])
        self.assertEqual(lv[2]["skip_price"], 70)  # decided on the crash bar's low, not its close
        self.assertEqual([x["sell_time"] for x in lv], [180, 120, None, 180])
        self.assertAlmostEqual(s["realized"], 0.5 + 0.6 + 0.8, places=9)
        self.assertEqual((s["fills"], s["skips"], s["sells"], s["outcome"]), (3, 1, 3, "completed"))

    def test_no_same_bar_buy_and_sell(self):
        r = ladder.simulate(levels4()[:1], [bar(0, 105, 120, 99, 110)], fee_pct=0)
        lv = r["levels"][0]
        self.assertEqual((lv["status"], lv["sell_status"]), ("filled", "placed"))
        self.assertAlmostEqual(r["summary"]["unrealized"], 10 / 100 * 110 - 10)

    def test_fees_come_off_what_is_received(self):
        r = ladder.simulate(levels4()[:1], [bar(0, 105, 106, 99, 100), bar(60, 100, 106, 100, 105)], fee_pct=0.2)
        qty = 10 / 100 * 0.998
        self.assertAlmostEqual(r["levels"][0]["pnl"], qty * 105 * 0.998 - 10)

    def test_trigger(self):
        bars = [bar(0, 110, 111, 104, 105), bar(60, 105, 106, 101, 102), bar(120, 102, 103, 99, 100)]
        never = ladder.simulate(levels4(), bars, trigger_price=95)
        self.assertEqual(never["summary"]["outcome"], "never_armed")
        armed = ladder.simulate(levels4(), bars, trigger_price=101.5, fee_pct=0)
        self.assertEqual((armed["summary"]["armed_at"], armed["summary"]["armed_price"]), (60, 101.5))
        self.assertEqual(armed["levels"][0]["fill_time"], 120)  # 101 never reached 100; the next bar did

    def test_no_data(self):
        self.assertEqual(ladder.simulate(levels4(), [])["summary"]["outcome"], "no_data")


class EngineTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())

    def candle(self, minute: datetime, lo: float, hi: float):
        Candle.objects.create(coin=self.coin, interval="1m", open_time=minute, open=Decimal(str(hi)), high=Decimal(str(hi)),
                              low=Decimal(str(lo)), close=Decimal(str(lo)), source=Candle.Source.REST)

    def make(self, levels=None, price=105.0, now=T, **kw):
        return engine.create(self.coin, Cascade.Kind.PERCENT, levels or levels4(), price=price, now=now, fee_pct=0, **kw)

    def lv(self, c, i) -> CascadeLevel:
        return c.levels.get(index=i)

    def test_create_places_level_one(self):
        c = self.make()
        self.assertEqual(c.status, Cascade.Status.ACTIVE)
        self.assertEqual(self.lv(c, 1).status, L.PLACED)
        self.assertEqual(c.levels.filter(status=L.PLACED).count(), 1)
        self.assertEqual(list(c.events.order_by("id").values_list("kind", flat=True)), ["created", "placed"])

    def test_misplaced_decimal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "misplaced decimal"):
            self.make(price=1050.0)
        self.assertFalse(Cascade.objects.exists())

    def test_fill_only_from_prices_after_the_order(self):
        c = self.make()
        self.candle(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc), lo=90, hi=105)  # opened before the order
        engine.step(c.pk, 101, T + timedelta(seconds=40))
        self.assertEqual(self.lv(c, 1).status, L.PLACED)
        self.candle(datetime(2026, 11, 1, 12, 1, tzinfo=timezone.utc), lo=99.5, hi=101)  # a wick between two steps
        engine.step(c.pk, 101, T + timedelta(seconds=100))
        l1 = self.lv(c, 1)
        self.assertEqual((l1.status, l1.fill_price, l1.sell_status), (L.FILLED, 100, S.PLACED))
        self.assertAlmostEqual(l1.sell_target, 105)
        self.assertEqual(self.lv(c, 2).status, L.PLACED)  # the next level only once level 1 bought
        self.assertEqual(c.levels.filter(status=L.PLACED).count(), 1)

    def test_sell_fills_on_a_later_step_and_completes(self):
        c = self.make(levels=levels4()[:1])
        engine.step(c.pk, 99, T + timedelta(minutes=1))  # buys; its sell is placed in the same step
        self.assertEqual(self.lv(c, 1).sell_status, S.PLACED)
        engine.step(c.pk, 104, T + timedelta(minutes=2))
        self.assertEqual(self.lv(c, 1).sell_status, S.PLACED)
        engine.step(c.pk, 105.5, T + timedelta(minutes=3))
        l1 = self.lv(c, 1)
        self.assertEqual(l1.sell_status, S.FILLED)
        self.assertAlmostEqual(l1.pnl, 0.5)
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.COMPLETED)
        self.assertEqual(engine.step(c.pk, 50, T + timedelta(minutes=4)), 0)  # finished: nothing happens

    def test_crash_skips_ahead_one_resting_at_a_time(self):
        c = self.make()
        engine.step(c.pk, 70, T + timedelta(minutes=1))
        self.assertEqual([self.lv(c, i).status for i in (1, 2, 3, 4)], [L.FILLED, L.SKIPPED, L.SKIPPED, L.PLACED])
        self.assertEqual(self.lv(c, 2).skip_price, 70)

    def test_trigger_waits_then_arms(self):
        c = self.make(levels=ladder.compute_levels(90, 10, 5, 0, 2, 5, 1), price=100, trigger_price=95)
        self.assertEqual(c.status, Cascade.Status.PENDING_TRIGGER)
        self.assertFalse(c.levels.filter(status=L.PLACED).exists())
        engine.step(c.pk, 96, T + timedelta(minutes=1))
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.PENDING_TRIGGER)
        self.candle(datetime(2026, 11, 1, 12, 2, tzinfo=timezone.utc), lo=94.5, hi=97)
        engine.step(c.pk, 96, T + timedelta(minutes=3))
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.ACTIVE)
        self.assertEqual(self.lv(c, 1).status, L.PLACED)

    def test_hold_and_resume(self):
        c = self.make()
        engine.step(c.pk, 99, T + timedelta(minutes=1))  # L1 bought, sell placed, L2 resting
        engine.hold(c)
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.HELD)
        self.assertEqual((self.lv(c, 1).sell_status, self.lv(c, 2).status), (S.NONE, L.PENDING))
        self.assertEqual(engine.step(c.pk, 50, T + timedelta(minutes=2)), 0)
        self.assertEqual(self.lv(c, 2).status, L.PENDING)
        engine.resume(c, 99)
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.ACTIVE)
        self.assertEqual((self.lv(c, 1).sell_status, self.lv(c, 2).status), (S.PLACED, L.PLACED))
        self.assertAlmostEqual(self.lv(c, 1).sell_target, 105)

    def test_cancel(self):
        c = self.make()
        engine.step(c.pk, 99, T + timedelta(minutes=1))
        engine.cancel(c)
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.CANCELLED)
        self.assertEqual([self.lv(c, i).status for i in (1, 2, 3, 4)], [L.FILLED, L.CANCELLED, L.CANCELLED, L.CANCELLED])
        self.assertEqual(self.lv(c, 1).sell_status, S.NONE)
        with self.assertRaises(ValueError):
            engine.cancel(c)

    def test_kill_switch_stops_new_buys_not_sells(self):
        c = self.make(levels=levels4()[:2])
        engine.step(c.pk, 99.5, T + timedelta(minutes=1))  # L1 bought, L2 placed
        engine.cancel_level(self.lv(c, 2), "buy")  # back to pending
        TradingControl.objects.update_or_create(pk=1, defaults={"halted": True})
        engine.step(c.pk, 99, T + timedelta(minutes=2))
        engine.step(c.pk, 99, T + timedelta(minutes=3))
        self.assertEqual(self.lv(c, 2).status, L.PENDING)
        self.assertEqual(c.events.filter(kind="blocked").count(), 1)
        engine.step(c.pk, 106, T + timedelta(minutes=4))
        self.assertEqual(self.lv(c, 1).sell_status, S.FILLED)
        c.refresh_from_db()
        self.assertTrue(c.last_error.startswith("Kill switch"))

    def test_conflicting_write_applies_nothing(self):
        c = self.make()
        real = engine.window

        def racing(*a, **kw):
            Cascade.objects.filter(pk=c.pk).update(version=F("version") + 1)  # another process got there first
            return real(*a, **kw)

        with mock.patch.object(engine, "window", side_effect=racing):
            self.assertEqual(engine.step(c.pk, 99, T + timedelta(minutes=1)), 0)
        self.assertEqual(self.lv(c, 1).status, L.PLACED)

    def test_edit_and_cancel_level(self):
        c = self.make()
        engine.edit_level(self.lv(c, 3), target_price=80, amount_usdt=20, take_profit_pct=9)
        l3 = self.lv(c, 3)
        self.assertEqual((l3.target_price, l3.amount_usdt, l3.take_profit_pct), (80, 20, 9))
        engine.edit_level(self.lv(c, 1), target_price=101)
        self.assertEqual(self.lv(c, 1).target_price, 101)
        with self.assertRaises(ValueError):
            engine.edit_level(self.lv(c, 2), amount_usdt=-1)
        engine.step(c.pk, 100.5, T + timedelta(minutes=1))  # buys at the edited 101
        engine.edit_level(self.lv(c, 1), sell_target=110)
        self.assertEqual(self.lv(c, 1).sell_target, 110)
        engine.cancel_level(self.lv(c, 1), "sell")
        self.assertEqual(self.lv(c, 1).sell_status, S.NONE)
        engine.step(c.pk, 100.5, T + timedelta(minutes=2))
        self.assertAlmostEqual(self.lv(c, 1).sell_target, 101 * 1.05)  # re-placed at the computed take-profit

    def test_step_all_and_stream_throttle(self):
        c = self.make()
        with mock.patch.object(engine, "live_price", return_value=99.0):
            self.assertGreater(engine.step_all(), 0)
        self.assertEqual(self.lv(c, 1).status, L.FILLED)
        with mock.patch.object(engine, "live_price", return_value=None):
            engine.step_all()
        c.refresh_from_db()
        self.assertIn("No live price", c.last_error)
        with mock.patch.object(engine, "step", return_value=1) as st:
            engine.on_price(self.coin, 95)
            engine.on_price(self.coin, 95)  # within the throttle window
        self.assertEqual(st.call_count, 1)


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("me", password="not-a-real-pass-123"))
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.form = {"coin": self.coin.pk, "kind": "percent", "fee_pct": "0.2", "base_price": "100", "base_amount": "10",
                     "price_step_pct": "1", "amount_step_pct": "20", "num_levels": "3", "tp_base_pct": "5", "tp_step_pct": "1"}

    def test_index_and_nav(self):
        r = self.client.get(reverse("cascades:index"))
        self.assertContains(r, "No cascades yet")
        self.assertContains(r, reverse("cascades:index"))

    @mock.patch.object(engine, "live_price", return_value=105.0)
    def test_preview_create_and_actions(self, _):
        r = self.client.post(reverse("cascades:create"), {**self.form, "action": "preview"})
        self.assertContains(r, "Preview")
        self.assertFalse(Cascade.objects.exists())
        r = self.client.post(reverse("cascades:create"), {**self.form, "action": "go"})
        c = Cascade.objects.get()
        self.assertRedirects(r, reverse("cascades:detail", args=[c.pk]))
        self.assertEqual(c.levels.count(), 3)
        self.assertContains(self.client.get(reverse("cascades:detail", args=[c.pk])), "Buy resting")
        self.client.post(reverse("cascades:action", args=[c.pk, "hold"]))
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.HELD)
        self.client.post(reverse("cascades:action", args=[c.pk, "resume"]))
        c.refresh_from_db()
        self.assertEqual(c.status, Cascade.Status.ACTIVE)
        lv = c.levels.get(index=2)
        self.client.post(reverse("cascades:level", args=[c.pk, lv.pk, "edit"]), {"amount_usdt": "33"})
        self.assertEqual(CascadeLevel.objects.get(pk=lv.pk).amount_usdt, 33)
        self.client.post(reverse("cascades:action", args=[c.pk, "delete"]))
        self.assertFalse(Cascade.objects.exists())

    @mock.patch.object(engine, "live_price", return_value=105.0)
    def test_manual_template_form(self, _):
        r = self.client.post(reverse("cascades:create"), {"coin": self.coin.pk, "kind": "manual", "fee_pct": "0.2",
                                                          "levels_text": "100, 10, 99", "action": "go"})
        self.assertContains(r, "higher than the buy price")
        self.client.post(reverse("cascades:create"), {"coin": self.coin.pk, "kind": "manual", "fee_pct": "0.2",
                                                      "levels_text": "100, 10, 105\n95, 12, 100", "action": "go"})
        self.assertEqual(Cascade.objects.get().kind, Cascade.Kind.MANUAL)

    def test_backtest_on_stored_candles(self):
        t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
        path = [(105, 106, 99, 100), (100, 101, 70, 75), (75, 96, 72, 95), (95, 106, 94, 100)]
        for i, (o, h, lo, c) in enumerate(path):
            Candle.objects.create(coin=self.coin, interval="1m", open_time=t0 + timedelta(minutes=i), open=o, high=h,
                                  low=lo, close=c, source=Candle.Source.REST)
        data = {**self.form, "price_step_pct": "10", "amount_step_pct": "0", "num_levels": "4", "fee_pct": "0",
                "interval": "1m", "start": "2026-10-01", "end": "2026-10-01", "action": "go"}
        r = self.client.post(reverse("cascades:backtest_create"), data)
        bt = CascadeBacktest.objects.get()
        self.assertRedirects(r, reverse("cascades:backtest_detail", args=[bt.pk]))
        self.assertEqual((bt.status, bt.fills, bt.bars), ("done", 3, 4))
        self.assertAlmostEqual(bt.realized, 1.9)
        self.assertContains(self.client.get(reverse("cascades:backtest_detail", args=[bt.pk])), "Start as dry-run cascade")
        r = self.client.get(reverse("cascades:create") + f"?from_backtest={bt.pk}")
        self.assertContains(r, 'name="price_step_pct" value="10.0"')  # the settings carried over

    def test_backtest_without_candles_explains(self):
        data = {**self.form, "interval": "5m", "start": "2026-10-01", "end": "2026-10-02", "action": "go"}
        self.client.post(reverse("cascades:backtest_create"), data)
        bt = CascadeBacktest.objects.get()
        self.assertEqual(bt.status, "failed")
        self.assertContains(self.client.get(reverse("cascades:backtest_detail", args=[bt.pk])), "Sync the coin first")
