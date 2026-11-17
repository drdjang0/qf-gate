"""Trade journal with reflection (roadmap 7.1): excursions, after-exit settlement, verdicts, lessons, page."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from market.models import Coin
from trading import journal as J
from trading.engine import Bar
from trading.models import BacktestRun, StrategyConfig, Trade

H = 3600
T0 = 1_790_000_000 - 1_790_000_000 % H


def bar(i, low, high, close=None):
    return Bar(time=T0 + i * H, open=(low + high) / 2, high=high, low=low, close=close if close is not None else (low + high) / 2, step=H)


def trade(entry_i, exit_i, entry, exit_, stop, pnl, reason, side="long", tag="", r=None):
    return {"entry_time": T0 + entry_i * H, "exit_time": T0 + exit_i * H, "entry_price": entry, "exit_price": exit_,
            "stop": stop, "side": side, "pnl": pnl, "exit_reason": reason, "entry_tag": tag,
            "r_multiple": r if r is not None else pnl / 5}


class SettleTests(TestCase):
    def test_winner_that_kept_running_is_exited_early(self):
        bars = [bar(0, 99, 101), bar(1, 98, 106), bar(2, 104, 110),  # in the trade: best 110, worst 98
                bar(3, 108, 112), bar(4, 109, 120), bar(5, 111, 115)]  # after the exit
        row = J.settle([trade(0, 2, 100, 110, 95, 10, "target")], bars, horizon=3)[0]
        self.assertEqual((row["risk"], row["r_basis"]), (5, "stop"))
        self.assertAlmostEqual(row["mfe_r"], 2.0)  # (110 - 100) / 5
        self.assertAlmostEqual(row["mae_r"], 0.4)  # (100 - 98) / 5
        self.assertAlmostEqual(row["run_after_r"], 2.0)  # (120 - 110) / 5
        self.assertEqual((row["settled"], row["verdict"], row["tone"]), (True, "exited early", "warn"))

    def test_stop_outs(self):
        bars = [bar(0, 96, 101), bar(1, 94, 99), bar(2, 95, 101.5), bar(3, 97, 100)]
        shaken = J.settle([trade(0, 1, 100, 95, 95, -5, "stop")], bars, horizon=2)[0]
        self.assertTrue(shaken["recovered"])
        self.assertEqual(shaken["verdict"], "shaken out")
        bars = [bar(0, 96, 101), bar(1, 94, 99), bar(2, 88, 96), bar(3, 84, 90)]
        saved = J.settle([trade(0, 1, 100, 95, 95, -5, "stop")], bars, horizon=2)[0]
        self.assertAlmostEqual(saved["drop_after_r"], (95 - 84) / 5)
        self.assertEqual(saved["verdict"], "stop saved")
        bars = [bar(0, 96, 101), bar(1, 94, 99), bar(2, 93, 97), bar(3, 92.5, 96)]
        self.assertEqual(J.settle([trade(0, 1, 100, 95, 95, -5, "stop")], bars, horizon=2)[0]["verdict"], "fair stop")

    def test_short_is_mirrored(self):
        bars = [bar(0, 99, 101), bar(1, 89, 100), bar(2, 88.5, 92), bar(3, 89, 93)]
        row = J.settle([trade(0, 1, 100, 90, 105, 10, "target", side="short")], bars, horizon=2)[0]
        self.assertAlmostEqual(row["mfe_r"], (100 - 89) / 5)
        self.assertAlmostEqual(row["mae_r"], (101 - 100) / 5)
        self.assertAlmostEqual(row["run_after_r"], (90 - 88.5) / 5)  # a fall continues a short
        self.assertEqual(row["verdict"], "good exit")

    def test_only_bars_after_the_exit_bar_and_within_the_horizon_count(self):
        bars = [bar(0, 99, 101), bar(1, 100, 130),  # spike inside the exit bar: not "after"
                bar(2, 109, 111), bar(3, 109, 112), bar(4, 100, 200)]  # bar 4 is beyond a 2-bar horizon
        row = J.settle([trade(0, 1, 100, 110, 95, 10, "target")], bars, horizon=2)[0]
        self.assertAlmostEqual(row["run_after_r"], (112 - 110) / 5)
        self.assertEqual(row["verdict"], "good exit")

    def test_waiting_end_of_test_and_no_stop(self):
        bars = [bar(0, 99, 101), bar(1, 100, 106), bar(2, 104, 108)]
        waiting = J.settle([trade(0, 1, 100, 105, 95, 5, "target")], bars, horizon=3)[0]
        self.assertEqual((waiting["settled"], waiting["bars_after"], waiting["verdict"]), (False, 1, "waiting"))
        ended = J.settle([trade(0, 1, 100, 105, 95, 5, "end of test")], bars, horizon=1)[0]
        self.assertEqual(ended["verdict"], "test ended")
        no_stop = J.settle([trade(0, 1, 100, 105, None, 5, "signal")], bars, horizon=1)[0]
        self.assertEqual((no_stop["risk"], no_stop["r_basis"]), (1.0, "1% of entry"))


class SummaryTests(TestCase):
    @staticmethod
    def row(verdict, pnl, reason, mfe=0.5, run=0.2, tag="a", r=None):
        return {"verdict": verdict, "settled": verdict != "waiting", "pnl": pnl, "exit_reason": reason, "entry_tag": tag,
                "r": r if r is not None else (1.0 if pnl > 0 else -1.0), "mfe_r": mfe, "mae_r": 0.3,
                "run_after_r": run, "r_basis": "stop"}

    def test_lessons_with_numbers(self):
        rows = ([self.row("shaken out", -5, "stop", mfe=1.2, tag="dip")] * 3 + [self.row("fair stop", -5, "stop", tag="dip")] * 3
                + [self.row("exited early", 10, "target", run=1.5, tag="break")] * 3 + [self.row("good exit", 10, "target", tag="break")] * 2
                + [self.row("waiting", 3, "target")])
        s = J.summarize(rows, 12, "1h")
        texts = " | ".join(x["text"] for x in s["lessons"])
        self.assertIn("3 of 6 (50%) stop-outs came back past the entry within 12 1h bars", texts)
        self.assertIn("3 of 5 (60%) winners kept going at least 1R after the exit (on average +1.5R", texts)
        self.assertIn("3 of 6 (50%) losing trades were up at least +1R first", texts)
        self.assertIn("Best entry type: break", texts)
        self.assertIn("1 recent trades are still waiting", texts)
        self.assertEqual((s["reviewed"], s["waiting"]), (11, 1))
        self.assertEqual({g["key"] for g in s["by_exit"]}, {"stop", "target"})

    def test_small_samples_draw_no_lesson(self):
        s = J.summarize([self.row("shaken out", -5, "stop")] * 2, 12)
        self.assertEqual([x["tone"] for x in s["lessons"]], ["info"])
        self.assertIn("No pattern stood out over 2 reviewed trades", s["lessons"][0]["text"])
        self.assertEqual(J.summarize([], 12)["lessons"][0]["text"], "No closed trades to review yet.")


class JournalPageTests(TestCase):
    def setUp(self):
        cache.clear()
        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC")
        cfg = StrategyConfig.objects.create(name="t", strategy_key="pdh_breakout", coin=coin, interval="1h")
        dt = lambda i: datetime.fromtimestamp(T0 + i * H, tz=timezone.utc)  # noqa: E731
        self.run = BacktestRun.objects.create(config=cfg, strategy_key="pdh_breakout", interval="1h", start=dt(0), end=dt(10),
                                             status=BacktestRun.Status.DONE)
        Trade.objects.create(backtest=self.run, entry_time=dt(0), exit_time=dt(2), entry_price=100, exit_price=110, qty=1,
                             stop=95, target=110, fees=0, pnl=10, r_multiple=2, exit_reason="target", entry_tag="breakout")
        self.client.force_login(User.objects.create_user("j", password="not-a-real-pass-123"))

    def test_fragment_and_card(self):
        bars = [bar(0, 99, 101), bar(1, 98, 106), bar(2, 104, 110), bar(3, 108, 112), bar(4, 109, 120)]
        with mock.patch("trading.data.load_bars", return_value=bars) as lb:
            page = self.client.get(reverse("trading:journal", args=["backtest", self.run.pk]) + "?h=4")
        self.assertEqual(lb.call_args.args[1], "1h")
        self.assertContains(page, "Trade journal")
        self.assertContains(page, "waiting")  # only 2 of the 4 bars after the exit exist
        self.assertContains(page, "breakout")
        detail = self.client.get(reverse("trading:backtest_detail", args=[self.run.pk]))
        self.assertContains(detail, reverse("trading:journal", args=["backtest", self.run.pk]))
        self.assertEqual(self.client.get(reverse("trading:journal", args=["nope", 1])).status_code, 404)
