"""Phase 6.1: shorts, leverage, liquidation, funding, mirrored short strategies."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from django.test import TestCase

from trading.backtest import simulate
from trading.engine import Bar, Engine
from trading.strategies.base import Signal, Strategy, get_strategy
from trading.tests.test_zone_strategy import day_bars

T0 = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
FUT = {"fee_rate": 0, "maker_fee_rate": 0, "slippage_bps": 0, "risk_pct": 100, "max_position_pct": 100,
       "starting_cash": 10_000, "market": "futures", "funding_default": 0}


class OpenOnce(Strategy):
    key = "open_once_test"
    params = {}

    def __init__(self, action="short", stop=105.0, target=None, at=0):
        super().__init__({})
        self.action, self.stop, self.target, self.at = action, stop, target, at

    def on_bar(self, ctx):
        if ctx.index == self.at and not ctx.position:
            return Signal(self.action, "t", stop=self.stop, target=self.target)
        return None


def h(i, o, hi, lo, c):
    return Bar(T0 + i * 3600, o, hi, lo, c, step=3600)


FLAT = (100, 100.5, 99.5, 100)


def run(bars, strat, **risk):
    eng = Engine(strat, {**FUT, **risk})
    out = []
    for i, b in enumerate(bars):
        out += eng.process(h(i, *b) if isinstance(b, tuple) else b)
    return eng, out


class ShortEngineTests(TestCase):
    def test_short_profit_and_stop(self):
        _e, t = run([FLAT, FLAT, (100, 100, 90, 92)], OpenOnce(target=95))
        self.assertEqual((t[0]["side"], t[0]["exit_reason"], t[0]["exit_price"]), ("short", "target", 95))
        self.assertAlmostEqual(t[0]["pnl"], t[0]["qty"] * 5)
        _e, t = run([FLAT, FLAT, (100, 107, 99, 106)], OpenOnce())
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("stop", 105))
        self.assertAlmostEqual(t[0]["pnl"], -t[0]["qty"] * 5)

    def test_spot_blocks_shorts(self):
        eng, t = run([FLAT, FLAT], OpenOnce(), market="spot")
        self.assertIsNone(eng.pos)
        self.assertEqual(eng.blocked, {"short needs futures": 1})

    def test_leverage_margin_and_liquidation(self):
        # 10x long, stop far below the liquidation price: liquidation comes first and costs the margin only
        eng, t = run([FLAT, FLAT, (100, 100, 85, 86)], OpenOnce("buy", stop=50), leverage=10, size_mode="fixed",
                     size_value=20_000, maint_margin_rate=0.005)
        self.assertEqual(t[0]["exit_reason"], "liquidation")
        self.assertAlmostEqual(t[0]["exit_price"], 100 * (1 - 0.1 + 0.005))
        self.assertAlmostEqual(eng.cash, 10_000 - 2_000, places=6)  # the 2,000 margin is gone, nothing more
        self.assertEqual(eng.liquidations, 1)

    def test_funding(self):
        bars = [h(i, *FLAT) for i in range(10)]  # 10 hours: crosses the 08:00 funding time once
        eng, _ = run(bars, OpenOnce("buy", stop=90), funding_default=0.001, size_mode="fixed", size_value=5_000)
        self.assertAlmostEqual(eng.funding_paid, 50 * 100 * 0.001)  # qty 50 x price 100 x 0.1%
        eng2, _ = run(bars, OpenOnce("short", stop=110), size_mode="fixed", size_value=5_000)
        eng3 = Engine(OpenOnce("short", stop=110), {**FUT, "size_mode": "fixed", "size_value": 5_000},
                      funding={T0 + 8 * 3600: 0.002})
        for b in bars:
            eng3.process(b)
        self.assertAlmostEqual(eng3.funding_paid, -50 * 100 * 0.002)  # shorts receive a positive rate

    def test_short_trailing_and_equity(self):
        _e, t = run([FLAT, FLAT, (100, 100, 80, 82), (82, 86, 82, 85)], OpenOnce(stop=110), trail_pct=5)
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("trailing stop", 84))
        eng, _ = run([FLAT, FLAT, (100, 100, 95, 96)], OpenOnce(stop=110), leverage=4, size_mode="fixed",
                     size_value=4_000)
        self.assertAlmostEqual(eng.equity(96), 10_000 + 40 * 4)  # margin back + 4 points x 40 units


class MirroredShortTests(TestCase):
    def test_short_twins_exist(self):
        for key in ("pdh_breakout_short", "pdl_reclaim_short", "zone_hotspot_short"):
            self.assertEqual(get_strategy(key).key, key)

    def test_mirror_trades_like_the_long_on_inverted_prices(self):
        """The short twin on prices P makes the same decisions as the long strategy on 1/P."""
        params = {"lookback_days": 3, "min_touches": 2, "min_held": 1, "one_trade_per_day": False}
        bars = [b for d in range(9) for b in day_bars(d)]
        inv = [replace(b, open=1 / b.open, high=1 / b.low, low=1 / b.high, close=1 / b.close) for b in bars]
        start = bars[0].time + 4 * 86400
        long_res = simulate(inv, "zone_hotspot", params, {**FUT, "market": "spot", "risk_pct": 1}, start)
        short_res = simulate(bars, "zone_hotspot_short", params, {**FUT, "risk_pct": 1}, start)
        self.assertGreater(long_res[2]["trades"], 0)
        lt = [(t["entry_time"], t["exit_reason"]) for t in long_res[0]]
        st = [(t["entry_time"], t["exit_reason"]) for t in short_res[0]]
        self.assertEqual(lt, st)
        self.assertTrue(all(t["side"] == "short" for t in short_res[0]))


class MlStrategyTests(TestCase):
    PARAMS = {"horizon": 3, "threshold_pct": 1.0, "prob_entry": 0.65, "prob_exit": 0, "min_train": 60,
              "train_size": 300, "retrain_bars": 12, "stop_pct": 3, "target_r": 1, "train_days": 3}

    def test_learns_a_repeating_pattern_without_look_ahead(self):
        from trading import checks

        # the synthetic day always falls into 06:00 then rises: hour of day predicts the rise
        bars = [b for d in range(14) for b in day_bars(d)]
        start = bars[0].time + 6 * 86400
        res = simulate(bars, "ml_logit", self.PARAMS, {**FUT, "market": "spot", "risk_pct": 1}, start)
        self.assertGreater(res[2]["trades"], 3)
        self.assertGreater(res[2]["win_rate_pct"], 60)
        la = checks.lookahead("ml_logit", self.PARAMS, bars, {**FUT, "market": "spot"}, cuts=10)
        self.assertTrue(la["ok"], la)

    def test_labels_use_only_later_bars(self):
        from trading.strategies.ml import MlLogit

        strat = MlLogit({**self.PARAMS, "horizon": 2, "min_train": 30})
        eng = Engine(strat, {**FUT, "market": "spot"})
        bars = [b for d in range(3) for b in day_bars(d)]
        seen = 0
        for b in bars:
            eng.process(b)
            st = eng.strat_state
            # every example still waiting is younger than the horizon
            self.assertTrue(all(eng.index - 1 - q[0] < 2 for q in st.get("queue", [])))
            seen = len(st.get("y", []))
        self.assertGreater(seen, 30)


from datetime import timedelta  # noqa: E402
from unittest import mock  # noqa: E402

from django.test import override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402

from market.models import Coin  # noqa: E402
from trading import bots as bots_mod  # noqa: E402
from trading import paper, signals_share  # noqa: E402
from trading.models import Bot, LiveSignal, PaperSession, StrategyConfig  # noqa: E402
from trading.tests.test_phase4 import TRADING, FakeBroker  # noqa: E402
from trading.tests.test_zone_strategy import T0 as Z0  # noqa: E402
from trading.tests.test_zone_strategy import ZonePaperMatchesBacktestTests  # noqa: E402

SHARE = {**TRADING, "SIGNAL_API_TOKEN": "s3cret"}


@override_settings(TRADING=SHARE)
class SignalSharingTests(TestCase):
    setUp = ZonePaperMatchesBacktestTests.setUp

    def test_paper_records_signals_and_api_serves_them(self):
        session = paper.start_session(self.cfg)
        PaperSession.objects.filter(pk=session.pk).update(started_at=Z0 + timedelta(days=4))
        now = Z0 + timedelta(days=4)
        while now < Z0 + timedelta(days=8):
            now += timedelta(minutes=60)
            paper.step_session(session.pk, now=now)
        local = LiveSignal.objects.filter(source="local")
        self.assertGreater(local.count(), 0)
        self.assertEqual(local.count(), local.values("bar_time", "action").distinct().count())  # no duplicates
        url = reverse("trading:api_signals")
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION="Bearer wrong").status_code, 401)
        data = self.client.get(url, {"since": 0}, HTTP_AUTHORIZATION="Bearer s3cret").json()["signals"]
        self.assertEqual(len(data), local.count())
        later = self.client.get(url, {"since": data[0]["id"]}, HTTP_AUTHORIZATION="Bearer s3cret").json()["signals"]
        self.assertEqual(len(later), len(data) - 1)

    def test_ingest_and_follow_bot(self):
        cfg = StrategyConfig.objects.create(name="follow", strategy_key="follow_remote", coin=self.coin, interval="1h")
        bot = bots_mod.start_bot(cfg)
        sig = {"id": 7, "config": "A", "pair": "BTC_USDT", "interval": "1h", "action": "buy", "stop": 90.0,
               "target": 120.0, "reason": "x", "bar_time": 1}
        fb = FakeBroker()
        with mock.patch("trading.bots._last_price", return_value=100.0):
            new = signals_share.ingest("alpha", [sig, sig, {"bad": 1}])
            self.assertEqual(len(new), 1)  # duplicates and junk are ignored
            self.assertEqual(signals_share.dispatch(broker=fb), 1)
            self.assertEqual(signals_share.dispatch(broker=fb), 0)  # acted once only
        self.assertEqual(Bot.objects.get(pk=bot.pk).pos["stop"], 90.0)
        r = self.client.post(reverse("trading:api_ingest"), data='{"source": "local", "signals": []}',
                             content_type="application/json", HTTP_AUTHORIZATION="Bearer s3cret")
        self.assertEqual(r.status_code, 400)  # nobody may pretend to be this instance
        r = self.client.post(reverse("trading:api_ingest"), data='{"source": "b", "signals": []}',
                             content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_short_strategies_cannot_start_bots(self):
        cfg = StrategyConfig.objects.create(name="s", strategy_key="zone_hotspot_short", coin=self.coin, interval="1h")
        with self.assertRaises(ValueError):
            bots_mod.start_bot(cfg)
