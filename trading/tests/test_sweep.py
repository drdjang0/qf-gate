from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.models import Candle, Coin, Job
from market.services import jobs
from market.services.sync import upsert_candles
from market.tests.fakes import synth_candle
from trading import sweep as sw
from trading.backtest import run_backtest
from trading.models import BacktestRun, StrategyConfig, Sweep
from trading.strategies.base import RISK_PARAMS, get_strategy


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class GridParsingTests(TestCase):
    def setUp(self):
        self.spec = sw.param_spec("pdh_breakout")

    def test_lists_ranges_and_keywords(self):
        p = self.spec["p.target_r"]
        self.assertEqual(sw.parse_values("1, 2, 2, 3", p, "target_r"), [1.0, 2.0, 3.0])
        self.assertEqual(sw.parse_values("0.5:2:0.5", p, "target_r"), [0.5, 1.0, 1.5, 2.0])
        self.assertEqual(sw.parse_values("0.1:0.3:0.1", p, "target_r"), [0.1, 0.2, 0.3])  # no float drift
        self.assertEqual(sw.parse_values("", p, "target_r"), [])
        self.assertEqual(sw.parse_values("all", self.spec["p.stop_mode"], "stop_mode"), ["pdh", "bar_low"])
        self.assertEqual(sw.parse_values("both", self.spec["p.one_trade_per_day"], "x"), [True, False])
        self.assertEqual(sw.parse_values("true", self.spec["p.one_trade_per_day"], "x"), [True])

    def test_errors(self):
        p = self.spec["p.target_r"]
        for bad in ("1:2", "3:1:1", "1:2:0", "a,b", "0.1:5000:0.1", "100"):
            with self.assertRaises(ValueError, msg=bad):
                sw.parse_values(bad, p, "target_r")
        with self.assertRaises(ValueError):
            sw.parse_values("bogus", self.spec["p.stop_mode"], "stop_mode")
        with self.assertRaises(ValueError):
            sw.build_grid("pdh_breakout", {"p.nope": "1"})

    def test_grid_and_counts(self):
        grid = sw.build_grid("pdh_breakout", {"p.target_r": "1,2,3", "r.risk_pct": "0.5, 1", "p.buffer_pct": ""})
        self.assertEqual(grid, {"p.target_r": [1.0, 2.0, 3.0], "r.risk_pct": [0.5, 1.0]})
        self.assertEqual(sw.combo_count(grid), 6)
        self.assertEqual(sw.varied_keys(grid), ["p.target_r", "r.risk_pct"])
        self.assertEqual(sw.split_combo({"p.target_r": 2, "r.risk_pct": 0.5}, {"buffer_pct": 0.1}, {}),
                         ({"buffer_pct": 0.1, "target_r": 2}, {"risk_pct": 0.5}))


class SweepRunTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.start = utc(2026, 10, 1)
        t0 = int(self.start.timestamp())
        candles = []
        for t in range(t0, t0 + 8 * 86400, 60):
            c = synth_candle(t, 60)
            drift = (t - t0) // 86400 * 400
            candles.append(replace(c, open=c.open + drift, high=c.high + drift + 3, low=c.low + drift, close=c.close + drift))
        upsert_candles(self.coin, "1m", candles, Candle.Source.REST)
        self.cfg = StrategyConfig.objects.create(
            name="t", strategy_key="pdh_breakout", coin=self.coin, interval="15m",
            params={"exit_at_day_end": False, "one_trade_per_day": False}, risk={},
        )

    def make(self, grid, oos_pct=30, **kw):
        return Sweep.objects.create(
            config=self.cfg, strategy_key="pdh_breakout", interval="15m", base_params=self.cfg.params,
            base_risk={}, grid=grid, start=self.start + timedelta(days=2), end=self.start + timedelta(days=8),
            oos_pct=oos_pct, **kw)

    def test_runs_ranks_and_splits(self):
        s = sw.run_sweep(self.make({"p.target_r": [1.0, 2.0, 4.0], "p.stop_mode": ["pdh", "bar_low"]}, min_trades=1))
        self.assertEqual((s.status, s.total, s.done, len(s.results)), ("done", 6, 6, 6))
        scores = [r["score"] for r in s.results if r["enough_trades"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(r["oos"] is not None for r in s.results))
        self.assertGreater(sum(r["is"]["trades"] for r in s.results), 0)
        hm = sw.heatmap(s)
        self.assertEqual((hm["x_key"], hm["y_key"], len(hm["rows"]), len(hm["xs"])), ("p.target_r", "p.stop_mode", 2, 3))

    def test_row_matches_a_normal_backtest(self):
        """Without an out-of-sample split, each row must equal a regular backtest of the same settings."""
        s = sw.run_sweep(self.make({"p.target_r": [1.5, 3.0]}, oos_pct=0, min_trades=0))
        for row in s.results:
            params, risk = sw.split_combo(row["combo"], s.base_params, s.base_risk)
            run = run_backtest(BacktestRun.objects.create(
                config=self.cfg, strategy_key=s.strategy_key, interval=s.interval, params=params, risk=risk,
                start=s.start, end=s.end))
            self.assertIsNone(row["oos"])
            for k in ("return_pct", "trades", "max_drawdown_pct"):
                self.assertAlmostEqual(row["is"][k], run.metrics[k], places=9, msg=k)

    def test_min_trades_ranks_thin_results_last(self):
        rows = [{"is": {"return_pct": 50, "trades": 1, "max_drawdown_pct": 1}},
                {"is": {"return_pct": 5, "trades": 10, "max_drawdown_pct": 1}}]
        ranked = sw.rank(rows, "return_pct", min_trades=5)
        self.assertEqual([r["is"]["return_pct"] for r in ranked], [5, 50])
        ranked = sw.rank([{"is": {"return_pct": 1, "trades": 9, "max_drawdown_pct": 0, "profit_factor": None}}],
                         "profit_factor", 0)
        self.assertEqual(ranked[0]["score"], sw.NO_SCORE)  # finite, JSON/Postgres safe

    def test_job_and_failure(self):
        s = self.make({"p.target_r": [1.0, 2.0]})
        jobs.enqueue(Job.Kind.SWEEP, self.coin, {"sweep_id": s.pk}, dedupe=False)
        job = jobs.run_job(jobs.claim_next())
        self.assertEqual(job.status, "done", job.message)
        self.assertIn("2 combinations", job.message)

        empty = Sweep.objects.create(config=self.cfg, strategy_key="pdh_breakout", interval="15m", base_params={},
                                     base_risk={}, grid={"p.target_r": [1.0, 2.0]},
                                     start=utc(2025, 1, 1), end=utc(2025, 1, 5))
        with self.assertRaises(ValueError):
            sw.run_sweep(empty)
        self.assertEqual(Sweep.objects.get(pk=empty.pk).status, "failed")


class SweepViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.client.force_login(self.user)
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.cfg = StrategyConfig.objects.create(owner=self.user, name="base", strategy_key="pdh_breakout", coin=self.coin,
                                                 interval="15m", params={"target_r": 2.0}, risk={"risk_pct": 1.0})

    def post(self, **grid):
        data = {"start": "2026-10-01", "end": "2026-10-08", "oos_pct": "30", "rank_by": "return_pct", "min_trades": "3"}
        data.update({f"g_{k.replace('.', '__')}": v for k, v in grid.items()})
        return self.client.post(reverse("trading:sweep_create", args=[self.cfg.pk]), data)

    def test_form_validation(self):
        self.assertContains(self.client.get(reverse("trading:sweep_create", args=[self.cfg.pk])), "Values to try")
        self.assertContains(self.post(), "two or more values")
        r = self.post(**{"p.target_r": "0.1:20:0.1", "p.buffer_pct": "0:1:0.1"})  # 200 x 11 = 2200
        self.assertContains(r, "maximum is")
        self.assertContains(self.post(**{"p.target_r": "1:2"}), "start:stop:step")
        self.assertFalse(Sweep.objects.exists())

    def test_create_detail_and_apply(self):
        with mock.patch("trading.views.sweep_mod.run_in_thread") as started:
            r = self.post(**{"p.target_r": "1, 3", "r.risk_pct": "0.5,1"})
        s = Sweep.objects.get()
        self.assertRedirects(r, reverse("trading:sweep_detail", args=[s.pk]), fetch_redirect_response=False)
        started.assert_called_once_with(s.pk)  # no worker alive -> runs in a thread
        self.assertEqual(s.grid, {"p.target_r": [1.0, 3.0], "r.risk_pct": [0.5, 1.0]})
        self.assertEqual(s.total, 4)

        s.results = sw.rank([
            {"combo": {"p.target_r": 3.0, "r.risk_pct": 0.5}, "is": {"return_pct": 4, "trades": 9, "max_drawdown_pct": 2,
             "win_rate_pct": 50, "profit_factor": 1.5}, "oos": {"return_pct": -1, "trades": 3, "profit_factor": 0.8}},
            {"combo": {"p.target_r": 1.0, "r.risk_pct": 1.0}, "is": {"return_pct": 1, "trades": 9, "max_drawdown_pct": 2,
             "win_rate_pct": 50, "profit_factor": 1.1}, "oos": {"return_pct": 2, "trades": 3, "profit_factor": 1.2}},
        ], "return_pct", 3)
        s.status = "done"
        s.save()
        page = self.client.get(reverse("trading:sweep_detail", args=[s.pk]))
        self.assertContains(page, "Does it hold up")
        self.assertContains(page, "Heatmap")
        self.assertContains(self.client.get(reverse("trading:strategy_detail", args=[self.cfg.pk])), f"#{s.pk}")

        r = self.client.post(reverse("trading:sweep_apply", args=[s.pk, 0]))
        new = StrategyConfig.objects.exclude(pk=self.cfg.pk).get()
        self.assertRedirects(r, reverse("trading:strategy_detail", args=[new.pk]), fetch_redirect_response=False)
        self.assertEqual(new.params["target_r"], 3.0)
        self.assertEqual(new.risk["risk_pct"], 0.5)
        self.assertEqual(StrategyConfig.objects.get(pk=self.cfg.pk).params["target_r"], 2.0)  # original untouched
        get_strategy(new.strategy_key)(new.params)  # valid
        self.assertTrue(set(new.risk) <= set(RISK_PARAMS))


class RankingGoalTests(TestCase):
    def test_new_metrics_and_scores(self):
        from trading.engine import compute_metrics

        day = 86400
        eq = [(i * day, v) for i, v in enumerate([10_000, 10_100, 10_050, 10_200, 10_150, 10_300])]
        m = compute_metrics([], eq, 10_000, 0, 6, None)
        self.assertGreater(m["sortino"], m["sharpe"])  # only the down days count as risk
        self.assertGreater(m["calmar"], 0)
        flat = compute_metrics([], [(0, 10_000), (day, 10_000)], 10_000, 0, 2, None)
        self.assertIsNone(flat["calmar"])
        row = {"is": {"return_pct": 6, "max_drawdown_pct": 2, "trades": 30, "win_rate_pct": 50, "sortino": 3.0}}
        self.assertAlmostEqual(sw.score(row, "combined"), 6 / 2 * 1.0 * 1.0)
        self.assertEqual(sw.score(row, "sortino"), 3.0)
        few = {"is": {**row["is"], "trades": 3}}
        self.assertLess(sw.score(few, "combined"), sw.score(row, "combined"))  # small samples score lower


class SmartSearchAndWalkForwardTests(TestCase):
    setUp = SweepRunTests.setUp
    make = SweepRunTests.make

    def test_smart_search_finds_the_best_with_fewer_runs(self):
        grid = {"x": list(range(20)), "y": list(range(20))}  # 400 combinations, best at x=13, y=7

        def evaluate(c):
            ret = 100 - (c["x"] - 13) ** 2 - (c["y"] - 7) ** 2
            return {"combo": c, "is": {"return_pct": ret, "trades": 10, "max_drawdown_pct": 1}, "oos": None}

        rows = sw.smart_search(grid, 120, evaluate, "return_pct", 0, seed=3)
        self.assertLessEqual(len(rows), 120)
        best = sw.rank(rows, "return_pct", 0)[0]["combo"]
        self.assertEqual((best["x"], best["y"]), (13, 7))
        again = sw.smart_search(grid, 120, evaluate, "return_pct", 0, seed=3)
        self.assertEqual([r["combo"] for r in rows], [r["combo"] for r in again])  # reproducible

    def test_smart_sweep_run(self):
        s = self.make({"p.target_r": [1.0, 1.5, 2.0, 3.0, 4.0], "p.stop_mode": ["pdh", "bar_low"]},
                      method="smart", budget=6, min_trades=0)
        s = sw.run_sweep(s)
        self.assertEqual((s.status, len(s.results)), ("done", 6))

    def test_walk_forward(self):
        s = sw.run_sweep(self.make({"p.target_r": [1.0, 2.0, 4.0]}, oos_pct=0, min_trades=0, wf_folds=2))
        wf = s.walkforward
        self.assertEqual([f["fold"] for f in wf["folds"]], [1, 2])
        for f in wf["folds"]:
            self.assertLess(f["train"][1], f["test"][1])
            self.assertEqual(f["train"][1], f["test"][0])  # tested right after the window it was picked on
        self.assertEqual(s.total, 3 * 3)
        user = User.objects.create_user("wf", password="not-a-real-pass-123")
        StrategyConfig.objects.update(owner=user)
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("trading:sweep_detail", args=[s.pk])), "Walk-forward")
