"""Zone hotspot strategy: entries, no look-ahead, warm-up, and paper == backtest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone as dj_tz

from market.exchanges.base import CandleData
from market.models import Candle, Coin
from market.services.sync import upsert_candles
from trading import paper
from trading.backtest import run_backtest, warmup
from trading.engine import Bar, Engine
from trading.models import BacktestRun, PaperSession, StrategyConfig, Trade
from trading.strategies.base import get_strategy

NO_COSTS = {"fee_rate": 0, "slippage_bps": 0, "risk_pct": 1, "max_position_pct": 100, "starting_cash": 10_000}
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
PARAMS = {"lookback_days": 3, "min_touches": 2, "min_held": 1}


def path(hour: float, top: float = 110.0) -> float:
    """One day: 105 -> 100 at 06:00 -> `top` at 18:00 -> back towards 105."""
    if hour <= 6:
        return 105 - 5 * hour / 6
    if hour <= 18:
        return 100 + (top - 100) * (hour - 6) / 12
    return top - (top - 105) * (hour - 18) / 6


def day_bars(day: int, top: float = 110.0, shift: float = 0.0) -> list[Bar]:
    out = []
    for h in range(24):
        o, c = path(h, top) + shift, path(h + 1, top) + shift
        t = int((T0 + timedelta(days=day, hours=h)).timestamp())
        out.append(Bar(t, o, max(o, c) + 0.1, min(o, c) - 0.1, c, step=3600))
    return out


def run(bars: list[Bar], params: dict) -> list[dict]:
    eng = Engine(get_strategy("zone_hotspot")({**PARAMS, **params}), NO_COSTS)
    trades = []
    for b in bars:
        trades += eng.process(b)
    return trades


class ZoneStrategyTests(TestCase):
    def test_bounce_trades_once_bands_exist(self):
        bars = [b for d in range(8) for b in day_bars(d)]
        trades = run(bars, {})
        self.assertGreater(len(trades), 0)
        first = datetime.fromtimestamp(trades[0]["entry_time"], timezone.utc)
        self.assertEqual(first.date(), (T0 + timedelta(days=3)).date())  # 3 completed days needed for 2 touches
        self.assertIn("bounce off zone", trades[0]["entry_reason"])
        self.assertEqual(trades[0]["exit_reason"], "target")  # next band above
        self.assertGreater(trades[0]["pnl"], 0)

    def test_no_look_ahead(self):
        """Changing the future must not change any trade decided before it."""
        base = [b for d in range(5) for b in day_bars(d)]
        calm = base + [b for d in range(5, 8) for b in day_bars(d)]
        crash = base + [b for d in range(5, 8) for b in day_bars(d, top=60, shift=-40)]
        cut = int((T0 + timedelta(days=5)).timestamp())
        before = lambda ts: [t for t in ts if t["entry_time"] < cut]  # noqa: E731
        self.assertEqual(before(run(calm, {})), before(run(crash, {})))
        self.assertGreater(len(before(run(calm, {}))), 0)

    def test_breakout_mode_and_minimum_room(self):
        bars = [b for d in range(5) for b in day_bars(d)] + day_bars(5, top=118)
        trades = run(bars, {"entry_mode": "breakout", "one_trade_per_day": False})
        self.assertTrue(any("close above zone" in t["entry_reason"] for t in trades))
        # with no band above, the target falls back to target_r; demanding more room than that blocks entries
        none = run(bars, {"entry_mode": "breakout", "target_mode": "r", "target_r": 1, "min_target_r": 5})
        self.assertEqual(none, [])

    def test_state_is_json_and_bounded(self):
        eng = Engine(get_strategy("zone_hotspot")(PARAMS), NO_COSTS)
        for b in [b for d in range(8) for b in day_bars(d)]:
            eng.process(b)
        st = json.loads(json.dumps(eng.state()))
        days = st["strategy"]["days"]
        self.assertEqual(len(days), 3)  # lookback_days
        self.assertLess(days[-1][0], (T0 + timedelta(days=7)).date().isoformat())  # only completed days

    def test_warmup(self):
        self.assertEqual(warmup("zone_hotspot", {"lookback_days": 30}), timedelta(days=31))
        self.assertEqual(warmup("pdh_breakout", {}), timedelta(days=2))


class ZonePaperMatchesBacktestTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        candles = []
        for m in range(9 * 1440):
            h = (m % 1440) / 60
            o, c = path(h), path(h + 1 / 60)
            candles.append(CandleData(T0 + timedelta(minutes=m), Decimal(str(round(o, 6))),
                                      Decimal(str(round(max(o, c) + 0.05, 6))), Decimal(str(round(min(o, c) - 0.05, 6))),
                                      Decimal(str(round(c, 6))), Decimal(1), Decimal(100), True))
        upsert_candles(self.coin, "1m", candles, Candle.Source.REST)
        self.cfg = StrategyConfig.objects.create(name="z", strategy_key="zone_hotspot", coin=self.coin,
                                                 interval="1h", params={**PARAMS, "one_trade_per_day": False}, risk={})

    def test_paper_in_small_steps_matches_backtest(self):
        start, end = T0 + timedelta(days=4), T0 + timedelta(days=9)
        session = paper.start_session(self.cfg)
        PaperSession.objects.filter(pk=session.pk).update(started_at=start)
        now = start
        while now < end:
            now += timedelta(minutes=137)
            s = PaperSession.objects.get(pk=session.pk)
            s.state = json.loads(json.dumps(s.state))
            s.save(update_fields=["state"])
            paper.step_session(session.pk, now=now)
        p = [(t.entry_time, t.exit_time, round(t.pnl, 6)) for t in Trade.objects.filter(paper=session)]
        bt = run_backtest(BacktestRun.objects.create(config=self.cfg, strategy_key="zone_hotspot", interval="1h",
                                                     params=self.cfg.params, risk={}, start=start, end=end))
        b = [(t.entry_time, t.exit_time, round(t.pnl, 6)) for t in bt.trades.exclude(exit_reason="end of test")]
        self.assertGreater(len(b), 0)
        self.assertEqual(p, b)


class SignalRecordingTests(ZonePaperMatchesBacktestTests):
    def test_paper_in_small_steps_matches_backtest(self):  # not re-run here
        pass

    def test_backtest_records_signals_and_plot(self):
        from trading.models import BacktestRun

        start, end = T0 + timedelta(days=4), T0 + timedelta(days=9)
        run = run_backtest(BacktestRun.objects.create(config=self.cfg, strategy_key="zone_hotspot", interval="1h",
                                                      params=self.cfg.params, risk={}, start=start, end=end))
        self.assertEqual(set(run.plot), {"zone support", "zone resistance"})
        self.assertTrue(all(start.timestamp() <= t for t, _ in run.plot["zone support"]))
        queued = [s for s in run.signals if s["status"] == "queued"]
        self.assertEqual(len(queued), run.trades.count())  # every queued signal became a trade
        self.assertTrue(all(s["tag"] == "zone_bounce" for s in queued))

        from django.contrib.auth.models import User
        from django.urls import reverse

        u = User.objects.create_user("p", password="not-a-real-pass-123")
        StrategyConfig.objects.update(owner=u)
        self.client.force_login(u)
        page = self.client.get(reverse("trading:backtest_detail", args=[run.pk]))
        self.assertContains(page, "Export signals")
        self.assertContains(page, "zone support")
        csv = self.client.get(reverse("trading:backtest_signals_csv", args=[run.pk]))
        self.assertEqual(csv.content.decode().splitlines()[0], "time_utc,action,status,reason,tag,stop,target")
