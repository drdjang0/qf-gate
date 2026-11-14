"""Phase 3.5: intra-bar detail, look-ahead / warm-up checks, protections, breakdowns."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from django.test import TestCase

from trading.engine import Bar, Engine
from trading.strategies.base import Signal, Strategy

NO_COSTS = {"fee_rate": 0, "maker_fee_rate": 0, "slippage_bps": 0, "risk_pct": 1, "max_position_pct": 100,
            "starting_cash": 10_000}
T0 = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())


class BuyOnce(Strategy):
    key = "buy_once_test"
    params = {}

    def __init__(self, stop=95.0, target=110.0, at=0):
        super().__init__({})
        self.stop, self.target, self.at = stop, target, at

    def on_bar(self, ctx):
        if ctx.index == self.at and not ctx.position:
            return Signal("buy", "test", stop=self.stop, target=self.target)
        return None


def hourly(i, o, h, low, c, detail=None):
    return Bar(T0 + i * 3600, o, h, low, c, step=3600, detail=detail)


class IntrabarDetailTests(TestCase):
    def _run(self, detail):
        eng = Engine(BuyOnce(), NO_COSTS)
        out = []
        out += eng.process(hourly(0, 100, 101, 99, 100))
        out += eng.process(hourly(1, 100, 101, 99, 100))  # entry at 100
        # big bar touching both the stop (95) and the target (110)
        out += eng.process(hourly(2, 100, 112, 94, 105, detail=detail))
        return eng, out

    def test_target_first_at_minute_level(self):
        t = T0 + 2 * 3600
        path = [(t, 100, 104, 99), (t + 60, 104, 111, 103), (t + 120, 108, 108, 94)]  # target, then stop
        eng, trades = self._run(path)
        self.assertEqual((trades[0]["exit_reason"], trades[0]["exit_price"]), ("target", 110))
        self.assertEqual(eng.detail_used, 1)

    def test_stop_first_at_minute_level(self):
        t = T0 + 2 * 3600
        path = [(t, 100, 100, 94), (t + 60, 96, 112, 96)]
        _eng, trades = self._run(path)
        self.assertEqual(trades[0]["exit_reason"], "stop")

    def test_without_detail_assumes_stop_first(self):
        eng, trades = self._run(None)
        self.assertEqual(trades[0]["exit_reason"], "stop")
        self.assertEqual(eng.detail_used, 0)

    def test_same_minute_is_still_worst_case_and_gaps_fill_at_minute_open(self):
        t = T0 + 2 * 3600
        _e, trades = self._run([(t, 100, 112, 94)])
        self.assertEqual(trades[0]["exit_reason"], "stop")
        _e, trades = self._run([(t, 100, 101, 99), (t + 60, 113, 113, 112), (t + 120, 100, 100, 94)])  # gaps above the target
        self.assertEqual((trades[0]["exit_reason"], trades[0]["exit_price"]), ("target", 113))

    def test_detail_is_not_saved_in_state(self):
        eng, _ = self._run([(T0, 100, 101, 99)])
        st = json.loads(json.dumps(eng.state()))
        self.assertTrue(all(b["detail"] is None for b in st["bars"]))
        Engine(BuyOnce(), NO_COSTS, state=st)  # restores fine


# -- 3.5.2 / 3.5.3 look-ahead and warm-up checks -------------------------------------------------

from trading import checks  # noqa: E402
from trading.strategies.base import REGISTRY, register  # noqa: E402
from trading.strategies.zones import ZoneHotspot  # noqa: E402
from trading.tests.test_zone_strategy import day_bars  # noqa: E402


class _PeekMemory(Strategy):
    """Cheats with memory shared between runs: 'knows' the next close from an earlier run."""

    key = "cheat_memory_test"
    params = {}
    SEEN: dict = {}

    def on_bar(self, ctx):
        _PeekMemory.SEEN[ctx.bar.time] = ctx.bar.close
        nxt = _PeekMemory.SEEN.get(ctx.bar.time + ctx.bar.step)
        if nxt is not None and nxt > ctx.bar.close:
            return Signal("buy", "peek", stop=ctx.bar.close * 0.9)
        return None


class _PeekDatabase(Strategy):
    key = "cheat_db_test"
    params = {}

    def on_bar(self, ctx):
        from market.models import Candle

        Candle.objects.filter(open_time__gt="2000-01-01").exists()  # could read future candles
        return None


class _ShortWarmupZones(ZoneHotspot):
    key = "zone_short_warmup_test"

    def warmup_days(self):
        return 1  # wrong on purpose: bands need lookback_days of history


for _cls in (_PeekMemory, _PeekDatabase, _ShortWarmupZones):
    if _cls.key not in REGISTRY:
        register(_cls)

ZONE = {"lookback_days": 3, "min_touches": 2, "min_held": 1}


def zone_bars(days=10):
    return [b for d in range(days) for b in day_bars(d)]


class LookaheadCheckTests(TestCase):
    def test_every_builtin_strategy_passes(self):
        bars = zone_bars()
        for key in ("pdh_breakout", "pdl_reclaim", "zone_hotspot"):
            params = ZONE if key == "zone_hotspot" else {}
            res = checks.lookahead(key, params, bars, NO_COSTS)
            self.assertTrue(res["ok"], (key, res))
        self.assertGreater(checks.lookahead("zone_hotspot", ZONE, bars, NO_COSTS)["signals"], 0)

    def test_memory_cheat_is_caught(self):
        _PeekMemory.SEEN.clear()
        res = checks.lookahead("cheat_memory_test", {}, zone_bars(4), NO_COSTS)
        self.assertFalse(res["ok"])
        self.assertTrue(res["mismatches"])

    def test_database_access_is_caught(self):
        res = checks.lookahead("cheat_db_test", {}, zone_bars(2), NO_COSTS)
        self.assertFalse(res["ok"])
        self.assertTrue(res["db_queries"])


class WarmupCheckTests(TestCase):
    def test_declared_warmup_is_enough(self):
        bars = zone_bars(14)
        start = bars[0].time + 8 * 86400
        res = checks.warmup("zone_hotspot", ZONE, bars, start, NO_COSTS)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["declared"], 4)

    def test_too_short_warmup_is_flagged(self):
        bars = [b for d in range(6) for b in day_bars(d)] + [b for d in range(6, 14) for b in day_bars(d, top=112)]
        start = bars[0].time + 8 * 86400
        res = checks.warmup("zone_short_warmup_test", ZONE, bars, start, NO_COSTS)
        self.assertFalse(res["ok"], res)


# -- 3.5.4 protections ------------------------------------------------------------------------------


class AlwaysBuy(Strategy):
    """Buys every bar when flat with a tight stop, so stop-outs are easy to produce."""

    key = "always_buy_test"
    params = {}

    def on_bar(self, ctx):
        if not ctx.position:
            return Signal("buy", "x", stop=ctx.bar.close - 1, target=ctx.bar.close + 50)
        return None


def falling(n, start=1000.0):
    """Each bar opens at the previous close and drops 3: every trade is stopped out next bar."""
    out, p = [], start
    for i in range(n):
        out.append(Bar(T0 + i * 3600, p, p + 0.5, p - 3, p - 3, step=3600))
        p -= 3
    return out


def protect(**risk):
    eng = Engine(AlwaysBuy({}), {**NO_COSTS, **risk})
    trades = []
    for b in falling(60):
        trades += eng.process(b)
    return eng, trades


class ProtectionTests(TestCase):
    def test_off_by_default(self):
        eng, trades = protect()
        self.assertEqual(eng.blocked, {})
        self.assertGreater(len(trades), 25)

    def test_cooldown(self):
        eng, trades = protect(cooldown_bars=5)
        base = len(protect()[1])
        self.assertLess(len(trades), base)
        self.assertGreater(eng.blocked.get("cooldown", 0), 0)
        gaps = [b["entry_time"] - a["exit_time"] for a, b in zip(trades, trades[1:])]
        self.assertTrue(all(g >= 5 * 3600 for g in gaps))  # signal blocked for 5 bars, filled at the next open

    def test_stoploss_guard(self):
        eng, trades = protect(sl_guard_trades=3, sl_guard_window_bars=20, sl_guard_pause_bars=10)
        self.assertGreater(eng.blocked.get("stoploss guard", 0), 0)
        self.assertLess(len(trades), len(protect()[1]))

    def test_max_drawdown(self):
        eng, _ = protect(max_dd_pct=1, max_dd_pause_bars=1000)
        self.assertIn("max drawdown", eng.blocked)
        self.assertGreaterEqual(eng.cash, 10_000 * 0.985)  # stopped soon after the 1% drawdown

    def test_low_profit(self):
        eng, _ = protect(low_profit_trades=4, low_profit_min_pct=0, low_profit_pause_bars=10)
        self.assertGreater(eng.blocked.get("low profit", 0), 0)

    def test_state_round_trip_keeps_locks(self):
        eng = Engine(AlwaysBuy({}), {**NO_COSTS, "cooldown_bars": 50})
        bars = falling(10)
        for b in bars[:5]:
            eng.process(b)
        again = Engine(AlwaysBuy({}), {**NO_COSTS, "cooldown_bars": 50}, state=json.loads(json.dumps(eng.state())))
        self.assertEqual((again.lock_until, again.lock_reason), (eng.lock_until, eng.lock_reason))


# -- 3.5.5 breakdowns ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from trading import report  # noqa: E402


def trade(exit_day, pnl, reason="target", tag="zone_bounce"):
    t = datetime(2026, 10, 1, 12, tzinfo=timezone.utc) + timedelta(days=exit_day)
    return SimpleNamespace(entry_time=t - timedelta(hours=1), exit_time=t, pnl=pnl, r_multiple=pnl / 100, fees=1.0,
                           exit_reason=reason, entry_tag=tag, entry_reason="x", entry_price=1, exit_price=1, qty=1,
                           stop=None, target=None)


class BreakdownTests(TestCase):
    def test_groups(self):
        ts = [trade(0, 100), trade(1, -50, "stop"), trade(35, 30, tag="zone_breakout")]
        b = report.breakdowns(ts, 10_000, "UTC")
        self.assertEqual([(r["key"], r["trades"], r["pnl"]) for r in b["month"]], [("2026-10", 2, 50), ("2026-11", 1, 30)])
        self.assertEqual({r["key"]: r["trades"] for r in b["exit"]}, {"stop": 1, "target": 2})
        self.assertEqual(b["exit"][0]["key"], "stop")  # worst first
        self.assertEqual({r["key"] for r in b["tag"]}, {"zone_bounce", "zone_breakout"})
        self.assertAlmostEqual(b["month"][0]["ret_pct"], 0.5)
        self.assertEqual(len(b["day"]), 3)
        self.assertTrue(report.workbook("t", ts, 10_000, {"a": 1, "blocked": {"cooldown": 2}}).startswith(b"PK"))

    def test_engine_tags_trades(self):
        eng = Engine(BuyOnce(), NO_COSTS)
        out = []
        for i in range(3):
            out += eng.process(hourly(i, 100, 120 if i == 2 else 101, 99, 100))
        self.assertEqual(out[0]["entry_tag"], "buy_once_test")  # falls back to the strategy key


# -- 3.6.1 exit management ------------------------------------------------------------------------


def run_exits(bars, stop=95.0, target=None, **risk):
    eng = Engine(BuyOnce(stop=stop, target=target), {**NO_COSTS, "risk_pct": 100, **risk})
    out = []
    for i, (o, h, low, c) in enumerate(bars):
        out += eng.process(hourly(i, o, h, low, c))
    return eng, out


FLAT = (100, 100.5, 99.5, 100)  # signal bar / entry bar (entry at 100)


class ExitManagementTests(TestCase):
    def test_trailing_stop(self):
        _e, t = run_exits([FLAT, FLAT, (100, 120, 116, 118), (118, 118, 113, 113)], trail_pct=5)
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("trailing stop", 114))

    def test_trailing_waits_for_activation(self):
        _e, t = run_exits([FLAT, FLAT, (100, 110, 108, 109), (109, 109, 104, 104)], trail_pct=5, trail_activate_r=3)
        self.assertEqual(t, [])  # +2R only: still on the original stop

    def test_trailing_never_uses_a_later_high_in_the_same_bar(self):
        _e, t = run_exits([FLAT, FLAT, (100, 120, 99, 118)], trail_pct=5)  # dips to 99 and peaks in one bar
        self.assertEqual(t, [])  # stop is still 95 during this bar

    def test_breakeven(self):
        _e, t = run_exits([FLAT, FLAT, (100, 106, 101, 105), (105, 105, 99, 99)], breakeven_r=1)
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("breakeven", 100))

    def test_partial_take_profit(self):
        eng, t = run_exits([FLAT, FLAT, (100, 106, 101, 105), (105, 111, 104, 110)], target=110,
                           partial_tp_r=1, partial_tp_pct=40)
        self.assertEqual([x["exit_reason"] for x in t], ["partial take-profit", "target"])
        self.assertAlmostEqual(t[0]["qty"] / (t[0]["qty"] + t[1]["qty"]), 0.4)
        self.assertEqual((t[0]["exit_price"], t[1]["exit_price"]), (105, 110))
        self.assertIsNone(eng.pos)

    def test_roi_fixed_and_decaying(self):
        _e, t = run_exits([FLAT, FLAT, (100, 104, 101, 103)], roi_pct=3)
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("roi", 103))
        # 10% decaying to 0 over 4 bars: two bars after entry the required gain is 5%
        _e, t = run_exits([FLAT, FLAT, (100, 104, 101, 103), (103, 105.5, 102, 105)], roi_pct=10, roi_decay_bars=4)
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("roi", 105))

    def test_time_exit(self):
        _e, t = run_exits([FLAT, FLAT, (100, 101, 99, 100.5), (101, 102, 100, 101)], max_hold_bars=2)
        self.assertEqual((t[0]["exit_reason"], t[0]["exit_price"]), ("time exit", 101))

    def test_trailing_with_minute_detail(self):
        t2 = T0 + 2 * 3600
        detail = [(t2, 100, 120, 100), (t2 + 60, 119, 119, 113)]  # peak, then falls through the new trailing stop
        eng = Engine(BuyOnce(stop=95, target=None), {**NO_COSTS, "risk_pct": 100, "trail_pct": 5})
        out = []
        for i, b in enumerate([FLAT, FLAT]):
            out += eng.process(hourly(i, *b))
        out += eng.process(hourly(2, 100, 120, 100, 113, detail=detail))
        self.assertEqual((out[0]["exit_reason"], out[0]["exit_price"]), ("trailing stop", 114))


# -- 3.6.5 limit (maker) orders --------------------------------------------------------------------


class LimitOrderTests(TestCase):
    def run_limit(self, bars, **risk):
        eng = Engine(BuyOnce(stop=90, target=None),
                     {**NO_COSTS, "risk_pct": 100, "entry_order": "limit", "limit_offset_pct": 1, **risk})
        out = []
        for i, b in enumerate(bars):
            out += eng.process(hourly(i, *b) if isinstance(b, tuple) else b)
        return eng, out

    def test_fills_only_when_price_trades_through(self):
        # signal close 100 -> limit 99; next bar only touches 99 (not through), the one after trades to 98.5
        eng, _ = self.run_limit([(100, 100.5, 99.5, 100), (100, 100.2, 99.0, 99.5), (99.5, 99.6, 98.5, 99)])
        self.assertEqual(eng.pos["entry_price"], 99)
        self.assertEqual(eng.pos["entry_time"], T0 + 2 * 3600)

    def test_better_open_fills_at_open_and_maker_fee(self):
        eng, _ = self.run_limit([(100, 100.5, 99.5, 100), (97, 98, 96, 97)], fee_rate=0.002, maker_fee_rate=0.0005)
        self.assertEqual(eng.pos["entry_price"], 97)
        self.assertAlmostEqual(eng.pos["entry_fee"], eng.pos["qty"] * 97 * 0.0005)

    def test_expires(self):
        eng, _ = self.run_limit([(100, 100.5, 99.5, 100)] + [(100, 101, 99.5, 100)] * 4, limit_expiry_bars=2)
        self.assertIsNone(eng.pos)
        self.assertEqual(eng.expired, 1)

    def test_fill_minute_then_exits_from_there(self):
        t1 = T0 + 3600
        detail = [(t1, 100, 100, 95), (t1 + 60, 96, 99.5, 98.5), (t1 + 120, 99, 99, 98)]
        # the first minute already trades below the 99 limit -> filled at 99 in minute 0
        eng, _ = self.run_limit([(100, 100.5, 99.5, 100), Bar(t1, 100, 100, 95, 98, step=3600, detail=detail)])
        self.assertEqual(eng.pos["entry_price"], 99)

    def test_target_exit_at_maker_fee(self):
        eng = Engine(BuyOnce(stop=95, target=105), {**NO_COSTS, "risk_pct": 100, "slippage_bps": 10,
                                                    "fee_rate": 0.002, "maker_fee_rate": 0.0, "exit_targets_maker": True})
        out = []
        for i, b in enumerate([FLAT, FLAT, (100, 106, 101, 105)]):
            out += eng.process(hourly(i, *b))
        self.assertEqual(out[0]["exit_price"], 105)  # no slippage on a resting limit


# -- 3.6.6 sizing modes and scaling in -------------------------------------------------------------


class SizingTests(TestCase):
    def entry(self, **risk):
        eng = Engine(BuyOnce(stop=95, target=None), {**NO_COSTS, **risk})
        for i, b in enumerate([FLAT, FLAT]):
            eng.process(hourly(i, *b))
        return eng

    def test_modes(self):
        self.assertAlmostEqual(self.entry(risk_pct=1).pos["qty"], 100 / 5)        # lose 1% (100) over 5 points
        self.assertAlmostEqual(self.entry(size_mode="fixed", size_value=2500).pos["qty"], 25)
        self.assertAlmostEqual(self.entry(size_mode="equity_pct", size_value=30).pos["qty"], 30)
        # caps still apply
        self.assertAlmostEqual(self.entry(size_mode="fixed", size_value=50_000, max_position_pct=40).pos["qty"], 40)

    def test_scaling_in_averages_down_within_caps(self):
        eng = Engine(BuyOnce(stop=90, target=None), {**NO_COSTS, "size_mode": "equity_pct", "size_value": 20,
                                                     "add_max": 2, "add_step_pct": 2, "max_position_pct": 50})
        for i, b in enumerate([FLAT, FLAT, (100, 100, 97, 97.5), (97.5, 97.5, 95.5, 96)]):
            eng.process(hourly(i, *b))
        pos = eng.pos
        self.assertEqual(pos["adds"], 2)
        # 2nd add is cut by the 50% max-position cap: room = 50% of equity at 96.04 minus what is held
        q3 = (6040 + 40 * 96.04) * 0.5 / 96.04 - 40
        self.assertAlmostEqual(pos["qty"], 40 + q3, places=6)
        self.assertAlmostEqual(pos["entry_price"], (20 * 100 + 20 * 98 + q3 * 96.04) / (40 + q3), places=6)

    def test_no_add_below_the_stop(self):
        eng = Engine(BuyOnce(stop=99, target=None), {**NO_COSTS, "add_max": 3, "add_step_pct": 2})
        out = []
        for i, b in enumerate([FLAT, FLAT, (100, 100, 97, 97.5)]):
            out += eng.process(hourly(i, *b))
        self.assertEqual(out[0]["exit_reason"], "stop")  # stopped at 99 before the 98 add level


# -- 5.1 order flow ---------------------------------------------------------------------------------


class OrderFlowTests(TestCase):
    def test_archive_side_split(self):
        from market.services.archive import candles_from_trades
        from market.tests.fakes import utc

        lines = ["1790812800.1,1,100,2,1", "1790812810.2,2,101,0.5,2", "1790812870.0,3,99,1,2"]
        c = candles_from_trades(lines, utc(2026, 10, 1), utc(2026, 10, 2))
        self.assertEqual((float(c[0].buy_volume), float(c[0].sell_volume)), (2.0, 0.5))
        self.assertEqual((float(c[1].buy_volume), float(c[1].sell_volume)), (0.0, 1.0))

    def test_rest_candles_do_not_wipe_the_split(self):
        from decimal import Decimal

        from market.exchanges.base import CandleData
        from market.models import Candle, Coin
        from market.services.sync import upsert_candles
        from market.tests.fakes import utc

        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC")
        D = Decimal
        upsert_candles(coin, "1m", [CandleData(utc(2026, 10, 1), D(1), D(2), D(1), D(2), D(3), D(6), True, D(2), D(1))], "archive")
        upsert_candles(coin, "1m", [CandleData(utc(2026, 10, 1), D(1), D(2), D(1), D(2), D(3), D(6), True)], "rest")
        c = Candle.objects.get()
        self.assertEqual((c.buy_volume, c.sell_volume), (D(2), D(1)))

    def test_entry_filter(self):
        eng = Engine(BuyOnce(), {**NO_COSTS, "min_entry_delta_pct": 20})
        b0 = Bar(T0, 100, 101, 99, 100, step=3600, buy_vol=5, sell_vol=5)  # balanced: blocked
        eng.process(b0)
        self.assertIsNone(eng.pending)
        self.assertEqual(eng.blocked, {"order-flow filter": 1})
        eng2 = Engine(BuyOnce(), {**NO_COSTS, "min_entry_delta_pct": 20})
        eng2.process(Bar(T0, 100, 101, 99, 100, step=3600, buy_vol=7, sell_vol=3))  # +40%: allowed
        self.assertIsNotNone(eng2.pending)
        eng3 = Engine(BuyOnce(), {**NO_COSTS, "min_entry_delta_pct": 20})
        eng3.process(Bar(T0, 100, 101, 99, 100, step=3600))  # unknown flow: skipped, never guessed
        self.assertIsNone(eng3.pending)
        self.assertEqual(Bar(T0, 1, 1, 1, 1, buy_vol=7, sell_vol=3).delta, 4)
