from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.models import Candle, Coin, Job
from market.services import jobs
from market.services.sync import upsert_candles
from market.tests.fakes import synth_candle
from trading import paper
from trading.backtest import run_backtest
from trading.engine import Bar, Engine, compute_metrics
from trading.models import BacktestRun, PaperSession, StrategyConfig, Trade
from trading.strategies.base import Context, Param, Signal, Strategy, get_strategy

NO_COSTS = {"fee_rate": 0, "slippage_bps": 0, "risk_pct": 1, "max_position_pct": 100, "starting_cash": 10_000}


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def bar(t, o, h, l, c, step=3600):  # noqa: E741
    return Bar(time=int(t.timestamp()), open=o, high=h, low=l, close=c, step=step)


class Scripted(Strategy):
    """Test strategy: emits pre-scripted signals by bar index."""

    key = "scripted_test"
    params = {}

    def __init__(self, script):
        super().__init__({})
        self.script = script

    def on_bar(self, ctx: Context):
        self.seen = ctx
        return self.script.get(ctx.index)


class EngineTests(TestCase):
    def test_fill_next_open_and_sizing_by_risk(self):
        e = Engine(Scripted({0: Signal("buy", stop=95, target=120)}), NO_COSTS)
        d = utc(2026, 10, 1)
        e.process(bar(d, 100, 101, 99, 100))
        self.assertIsNone(e.pos)  # no same-bar fill: no look-ahead
        e.process(bar(d + timedelta(hours=1), 100, 102, 99, 101))
        self.assertEqual(e.pos["entry_price"], 100)
        self.assertAlmostEqual(e.pos["qty"], 20)  # 1% of 10k = 100 risk / (100-95)
        self.assertAlmostEqual(e.pos["risk_amount"], 100)

    def test_stop_first_when_both_hit_and_gap_fill(self):
        e = Engine(Scripted({0: Signal("buy", stop=95, target=105)}), NO_COSTS)
        d = utc(2026, 10, 1)
        e.process(bar(d, 100, 100, 100, 100))
        closed = e.process(bar(d + timedelta(hours=1), 100, 106, 94, 100))  # both levels touched
        self.assertEqual(closed[0]["exit_reason"], "stop")
        self.assertAlmostEqual(closed[0]["r_multiple"], -1.0)

        e = Engine(Scripted({0: Signal("buy", stop=95)}), NO_COSTS)
        e.process(bar(d, 100, 100, 100, 100))
        e.process(bar(d + timedelta(hours=1), 100, 100, 99, 99))
        closed = e.process(bar(d + timedelta(hours=2), 90, 91, 89, 90))  # gaps below the stop
        self.assertEqual(closed[0]["exit_price"], 90)
        self.assertAlmostEqual(closed[0]["r_multiple"], -2.0)

    def test_fees_and_slippage(self):
        e = Engine(Scripted({0: Signal("buy", stop=90, target=110)}),
                   {**NO_COSTS, "fee_rate": 0.001, "slippage_bps": 10, "risk_pct": 100, "max_position_pct": 10})
        d = utc(2026, 10, 1)
        e.process(bar(d, 100, 100, 100, 100))
        e.process(bar(d + timedelta(hours=1), 100, 100, 100, 100))
        self.assertAlmostEqual(e.pos["entry_price"], 100.1)
        qty = e.pos["qty"]
        self.assertAlmostEqual(qty * 100.1, 1000)  # capped at 10% of equity
        closed = e.process(bar(d + timedelta(hours=2), 100, 111, 100, 110))
        t = closed[0]
        self.assertAlmostEqual(t["exit_price"], 110 * 0.999)
        self.assertAlmostEqual(t["fees"], qty * 100.1 * 0.001 + qty * 110 * 0.999 * 0.001)
        self.assertAlmostEqual(e.cash, 10_000 + t["pnl"])

    def test_previous_day_levels_and_last_bar_flag(self):
        s = Scripted({})
        e = Engine(s, NO_COSTS)
        d = utc(2026, 10, 1)
        for h in range(24):
            e.process(bar(d + timedelta(hours=h), 100, 100 + h, 90 - h, 100))
        self.assertTrue(s.seen.is_last_bar_of_day)
        self.assertIsNone(s.seen.pdh)
        e.process(bar(d + timedelta(days=1), 100, 101, 99, 100))
        self.assertTrue(s.seen.is_new_day)
        self.assertEqual((s.seen.pdh, s.seen.pdl), (123, 67))

    def test_metrics(self):
        trades = [{"pnl": 200, "r_multiple": 2, "fees": 1}, {"pnl": -100, "r_multiple": -1, "fees": 1}]
        eq = [(int(utc(2026, 10, 1).timestamp()), 10_200), (int(utc(2026, 10, 2).timestamp()), 10_100)]
        m = compute_metrics(trades, eq, 10_000, 5, 10, 3.0)
        self.assertAlmostEqual(m["return_pct"], 1.0)
        self.assertEqual(m["win_rate_pct"], 50)
        self.assertEqual(m["profit_factor"], 2)
        self.assertAlmostEqual(m["max_drawdown_pct"], 100 / 10_200 * 100)
        self.assertEqual(m["exposure_pct"], 50)

    def test_param_validation(self):
        p = Param("float", 1.0, min=0, max=2)
        self.assertEqual(p.clean("x", "1.5"), 1.5)
        self.assertEqual(p.clean("x", None), 1.0)
        with self.assertRaises(ValueError):
            p.clean("x", 3)
        with self.assertRaises(ValueError):
            get_strategy("pdh_breakout")({"stop_mode": "nope"})


def day_bars(d: datetime, base: float, breakout_at: int | None):
    """24 hourly bars ranging base±10; optionally a breakout above PDH at hour `breakout_at`."""
    out = []
    for h in range(24):
        t = d + timedelta(hours=h)
        if breakout_at is not None and h == breakout_at:
            out.append(bar(t, base + 9, base + 13, base + 8.5, base + 12))
        elif breakout_at is not None and h > breakout_at:
            p = base + 12 + (h - breakout_at)
            out.append(bar(t, p, p + 1, p - 0.5, p + 0.5))
        else:
            out.append(bar(t, base, base + 10, base - 10, base))
    return out


class StrategyTests(TestCase):
    def test_pdh_breakout_enters_after_cross_and_exits_at_day_end(self):
        strat = get_strategy("pdh_breakout")({"buffer_pct": 0, "stop_buffer_pct": 0, "target_mode": "none"})
        e = Engine(strat, NO_COSTS)
        d = utc(2026, 10, 1)
        trades = []
        for b in day_bars(d, 100, None) + day_bars(d + timedelta(days=1), 100, 5) + day_bars(d + timedelta(days=2), 130, None)[:2]:
            trades += e.process(b)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["entry_time"], int((d + timedelta(days=1, hours=6)).timestamp()))  # next bar's open
        self.assertEqual(t["stop"], 110)  # PDH
        self.assertEqual(t["exit_reason"], "day end")
        self.assertEqual(t["exit_time"], int((d + timedelta(days=2)).timestamp()))

    def test_pdl_reclaim(self):
        strat = get_strategy("pdl_reclaim")({"sweep_pct": 0, "stop_buffer_pct": 0, "target_mode": "pdh"})
        e = Engine(strat, NO_COSTS)
        d = utc(2026, 10, 1)
        bars = [bar(d + timedelta(hours=h), 100, 110, 90, 100) for h in range(24)]  # PDL 90, PDH 110
        d2 = d + timedelta(days=1)
        bars += [bar(d2, 100, 100, 85, 88),                        # sweep below 90
                 bar(d2 + timedelta(hours=1), 88, 93, 87, 92),     # close back above 90 -> buy
                 bar(d2 + timedelta(hours=2), 92, 111, 92, 109)]   # reaches the PDH target
        trades = []
        for b in bars:
            trades += e.process(b)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["stop"], 85)
        self.assertEqual(trades[0]["exit_reason"], "target")
        self.assertEqual(trades[0]["exit_price"], 110)


class BacktestAndPaperTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.start = utc(2026, 10, 1)
        t0 = int(self.start.timestamp())
        candles = []
        for t in range(t0, t0 + 6 * 86400, 60):
            c = synth_candle(t, 60)
            drift = (t - t0) // 86400 * 400  # each day higher, so PDH breaks happen
            candles.append(replace(c, open=c.open + drift, high=c.high + drift + 3, low=c.low + drift, close=c.close + drift))
        upsert_candles(self.coin, "1m", candles, Candle.Source.REST)
        self.cfg = StrategyConfig.objects.create(
            name="t", strategy_key="pdh_breakout", coin=self.coin, interval="15m",
            params={"exit_at_day_end": False, "one_trade_per_day": False}, risk={},
        )

    def _run(self, **kw):
        run = BacktestRun.objects.create(
            config=self.cfg, strategy_key="pdh_breakout", interval="15m", params=self.cfg.params, risk={},
            start=self.start + timedelta(days=2), end=self.start + timedelta(days=6), **kw)
        return run

    def test_backtest_runs_and_stores_results(self):
        run = run_backtest(self._run())
        self.assertEqual(run.status, "done")
        self.assertEqual(run.bars, 4 * 96)
        self.assertGreater(run.metrics["trades"], 0)
        self.assertEqual(run.trades.count(), run.metrics["trades"])
        self.assertTrue(all(t.entry_time >= run.start for t in run.trades.all()))  # warm-up never trades
        self.assertLessEqual(len(run.equity), 1500)

    def test_backtest_via_job_queue(self):
        run = self._run()
        jobs.enqueue(Job.Kind.BACKTEST, self.coin, {"run_id": run.pk}, dedupe=False)
        job = jobs.run_job(jobs.claim_next())
        self.assertEqual(job.status, "done", job.message)
        self.assertIn("Backtest #", job.message)

    def test_paper_in_small_steps_matches_one_backtest(self):
        """Paper trading (state saved/restored between steps) must trade exactly like one continuous run."""
        session = paper.start_session(self.cfg)
        PaperSession.objects.filter(pk=session.pk).update(started_at=self.start + timedelta(days=2))
        now = self.start + timedelta(days=2)
        while now < self.start + timedelta(days=6):
            now += timedelta(minutes=97)  # odd step: bars arrive in uneven batches
            s = PaperSession.objects.get(pk=session.pk)
            s.state = json.loads(json.dumps(s.state))  # same round-trip as a real DB save
            s.save(update_fields=["state"])
            paper.step_session(session.pk, now=now)
        paper_trades = [(t.entry_time, t.exit_time, round(t.pnl, 6)) for t in Trade.objects.filter(paper=session)]

        run = run_backtest(self._run())
        bt_trades = [(t.entry_time, t.exit_time, round(t.pnl, 6)) for t in run.trades.exclude(exit_reason="end of test")]
        self.assertGreater(len(bt_trades), 0)
        self.assertEqual(paper_trades, bt_trades)

    def test_repeated_steps_do_not_double_count(self):
        session = paper.start_session(self.cfg)
        PaperSession.objects.filter(pk=session.pk).update(started_at=self.start + timedelta(days=2))
        end = self.start + timedelta(days=6)
        paper.step_session(session.pk, now=end)
        first = Trade.objects.filter(paper=session).count()
        self.assertEqual(paper.step_session(session.pk, now=end), 0)
        self.assertEqual(Trade.objects.filter(paper=session).count(), first)

    def test_broken_strategy_marks_session_error(self):
        session = paper.start_session(self.cfg)
        PaperSession.objects.filter(pk=session.pk).update(strategy_key="does_not_exist")
        paper.step_all()
        s = PaperSession.objects.get(pk=session.pk)
        self.assertEqual(s.status, "error")
        self.assertIn("Unknown strategy", s.last_error)


class TradingViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.client.force_login(self.user)
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())

    def test_create_backtest_and_paper_flow(self):
        self.assertContains(self.client.get(reverse("trading:strategy_create")), "PDH breakout")
        r = self.client.post(reverse("trading:strategy_create") + "?strategy=pdh_breakout", {
            "strategy": "pdh_breakout", "name": "BTC PDH", "coin": self.coin.pk, "interval": "15m",
            "p_buffer_pct": "0.1", "p_stop_mode": "bar_low", "p_target_mode": "r", "p_target_r": "3",
            "p_one_trade_per_day": "on", "r_risk_pct": "0.5",
        })
        self.assertEqual(r.status_code, 302, r.content[:3000])
        cfg = StrategyConfig.objects.get()
        self.assertEqual(cfg.params["stop_mode"], "bar_low")
        self.assertEqual(cfg.params["target_r"], 3.0)
        self.assertFalse(cfg.params["exit_at_day_end"])  # unchecked box
        self.assertEqual(cfg.risk["risk_pct"], 0.5)
        self.assertEqual(cfg.risk["fee_rate"], 0.002)  # default filled in

        bad = self.client.post(reverse("trading:strategy_edit", args=[cfg.pk]), {
            "name": "x", "coin": self.coin.pk, "interval": "15m", "p_target_r": "999", "p_stop_mode": "pdh",
            "p_target_mode": "r"})
        self.assertEqual(bad.status_code, 200)  # out of range -> form error, nothing saved

        self.assertEqual(self.client.get(reverse("trading:strategy_detail", args=[cfg.pk])).status_code, 200)
        # no candles stored: the backtest fails with a clear message instead of crashing
        r = self.client.post(reverse("trading:backtest_create", args=[cfg.pk]),
                             {"start": "2026-10-01", "end": "2026-10-05"}, follow=True)
        self.assertContains(r, "Sync the coin first")

        r = self.client.post(reverse("trading:paper_start", args=[cfg.pk]), follow=True)
        self.assertContains(r, "Paper #")
        s = PaperSession.objects.get()
        self.assertEqual(s.status, "running")
        r = self.client.post(reverse("trading:paper_start", args=[cfg.pk]), follow=True)
        self.assertContains(r, "already running")
        self.client.post(reverse("trading:paper_stop", args=[s.pk]))
        self.assertEqual(PaperSession.objects.get().status, "stopped")
        self.assertEqual(self.client.get(reverse("trading:strategy_list")).status_code, 200)
