"""Phase 5: portfolio backtests, screener, exchange comparison."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.exchanges.base import CandleData
from market.models import Candle, Coin
from market.services import compare, screener
from market.services.sync import upsert_candles
from trading.backtest import simulate
from trading.portfolio import simulate_portfolio
from trading.tests.test_zone_strategy import day_bars

NO_COSTS = {"fee_rate": 0, "maker_fee_rate": 0, "slippage_bps": 0, "risk_pct": 1, "max_position_pct": 50,
            "starting_cash": 10_000}
ZONE = {"lookback_days": 3, "min_touches": 2, "min_held": 1, "one_trade_per_day": False}


def coin_bars(shift=0.0, days=9):
    return [b for d in range(days) for b in day_bars(d, shift=shift)]


class PortfolioTests(TestCase):
    def test_single_coin_equals_a_normal_backtest(self):
        bars = coin_bars()
        start = bars[0].time + 4 * 86400
        single = simulate(bars, "zone_hotspot", ZONE, NO_COSTS, start)
        port = simulate_portfolio({"A": coin_bars()}, "zone_hotspot", ZONE, NO_COSTS, start, max_open=1)
        self.assertGreater(single[2]["trades"], 0)
        self.assertEqual(port["metrics"]["trades"], single[2]["trades"])
        self.assertAlmostEqual(port["metrics"]["return_pct"], single[2]["return_pct"], places=9)

    def test_shared_cash_and_max_open(self):
        start = coin_bars()[0].time + 4 * 86400
        res = simulate_portfolio({"A": coin_bars(), "B": coin_bars(shift=0.01)}, "zone_hotspot", ZONE, NO_COSTS,
                                 start, max_open=1)
        m = res["metrics"]
        self.assertGreater(m["blocked_max_open"], 0)  # both coins signal together; only one slot
        # never two positions at once: trades of A and B must not overlap in time
        spans = sorted((t["entry_time"], t["exit_time"]) for t in res["trades"])
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])))
        # portfolio equity = starting cash + every trade's P&L
        self.assertAlmostEqual(res["equity"][-1][1], 10_000 + sum(t["pnl"] for t in res["trades"]), places=6)
        both = simulate_portfolio({"A": coin_bars(), "B": coin_bars(shift=0.01)}, "zone_hotspot", ZONE, NO_COSTS,
                                  start, max_open=2)
        self.assertEqual(both["metrics"]["blocked_max_open"], 0)
        self.assertEqual(set(both["per_coin"]), {"A", "B"})


class ScreenerTests(TestCase):
    TICKERS = [
        {"currency_pair": "AAA_USDT", "last": "10", "highest_bid": "9.99", "lowest_ask": "10", "change_percentage": "5",
         "quote_volume": "5000000", "high_24h": "11", "low_24h": "9"},
        {"currency_pair": "BBB_USDT", "last": "1", "highest_bid": "0.9", "lowest_ask": "1", "change_percentage": "-2",
         "quote_volume": "9000000", "high_24h": "1.1", "low_24h": "0.95"},  # 10% spread
        {"currency_pair": "CCC_USDT", "last": "2", "highest_bid": "1.999", "lowest_ask": "2", "change_percentage": "1",
         "quote_volume": "100", "high_24h": "2.1", "low_24h": "1.9"},  # too little volume
        {"currency_pair": "DDD_BTC", "last": "1", "highest_bid": "1", "lowest_ask": "1", "quote_volume": "1e9"},
    ]

    def test_filters(self):
        rows = screener.rows_from_tickers(self.TICKERS, "USDT")
        self.assertEqual([r["pair"] for r in rows], ["AAA_USDT", "BBB_USDT", "CCC_USDT"])
        out = screener.screen(rows, screener.Filters())
        self.assertEqual([r["pair"] for r in out], ["AAA_USDT"])
        self.assertAlmostEqual(out[0]["range_pct"], (11 - 9) / 9 * 100)
        self.assertEqual(screener.screen(rows, screener.Filters(blacklist=("AAA",))), [])
        loose = screener.screen(rows, screener.Filters(min_volume=0, max_spread_pct=100, sort="change"))
        self.assertEqual([r["base"] for r in loose], ["AAA", "CCC", "BBB"])

    def test_page(self):
        from unittest import mock

        self.client.force_login(User.objects.create_user("s", password="not-a-real-pass-123"))
        with mock.patch("market.services.screener.get_client") as gc:
            gc.return_value.get_all_tickers.return_value = self.TICKERS
            page = self.client.get(reverse("market:screener"))
        self.assertContains(page, "AAA")
        self.assertContains(page, "Add coin")
        form = self.client.get(reverse("market:coin_create") + "?symbol=aaa&gate_currency=aaa&quote=usdt")
        self.assertContains(form, 'value="AAA"')


class CompareTests(TestCase):
    def test_same_and_different_asset(self):
        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        D = Decimal
        upsert_candles(coin, "1d", [CandleData(today - timedelta(days=i), D(100), D(101), D(99), D(100), D(1), D(100), True)
                                    for i in range(1, 4)], Candle.Source.REST)

        class Fake:
            def __init__(self, close):
                self.close = close

            def get_candles(self, symbol, interval, start, end):
                return [CandleData(today - timedelta(days=i), D(1), D(1), D(1), D(self.close), D(2), D(200), True, D(1.5), D(0.5))
                        for i in range(1, 4)]

        same = compare.compare_daily(coin, 10, client=Fake(100.05))
        self.assertEqual((same["symbol"], same["listed"], len(same["rows"])), ("BTCUSDT", True, 3))
        self.assertTrue(same["same_asset"])
        self.assertAlmostEqual(same["rows"][0]["other_delta_pct"], 50.0)
        self.assertAlmostEqual(same["volume_share_pct"], 100 / 300 * 100)
        other = compare.compare_daily(coin, 10, client=Fake(3.0))  # same ticker, different token
        self.assertFalse(other["same_asset"])
