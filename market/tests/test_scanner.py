"""Multi-exchange adapters + on-demand Scanner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.exchanges import public as P
from market.exchanges.base import CandleData
from market.models import Coin
from market.services import scanner

T = 1794700800  # a UTC hour


class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.ok = data, code, code == 200

    def json(self):
        return self._d


class OneShot:
    """Returns `data` once, then an empty page (ends pagination)."""

    def __init__(self, data):
        self.data, self.urls = data, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.urls.append((url, params))
        d, self.data = self.data, None
        return Resp(d if d is not None else ([] if "candlesticks" in url or "klines" in url else {"code": "x"}))


SAMPLES = {  # one hourly candle each, in the exchange's own format: open 10, high 12, low 9, close 11
    "gate": [[str(T), "1100", "11", "12", "9", "10", "100", "true"]],
    "binance": [[T * 1000, "10", "12", "9", "11", "100", (T + 3600) * 1000, "1100"]],
    "bybit": {"retCode": 0, "result": {"list": [[str(T * 1000), "10", "12", "9", "11", "100", "1100"]]}},
    "kucoin": {"code": "200000", "data": [[str(T), "10", "11", "12", "9", "100", "1100"]]},
    "mexc": [[T * 1000, "10", "12", "9", "11", "100", (T + 3600) * 1000, "1100"]],
    "bitget": {"code": "00000", "data": [[str(T * 1000), "10", "12", "9", "11", "100", "1100", "1100"]]},
    "okx": {"code": "0", "data": [[str(T * 1000), "10", "12", "9", "11", "100", "x", "1100", "1"]]},
}


class AdapterTests(TestCase):
    def test_every_exchange_maps_to_the_same_candle(self):
        start, end = datetime.fromtimestamp(T, timezone.utc), datetime.fromtimestamp(T + 3600, timezone.utc)
        for key, sample in SAMPLES.items():
            c = P.candles(key, "SOL", "USDT", "1h", start, end, session=OneShot(sample))
            self.assertEqual(len(c), 1, key)
            self.assertEqual((c[0].open, c[0].high, c[0].low, c[0].close), (Decimal(10), Decimal(12), Decimal(9), Decimal(11)), key)
            self.assertEqual((c[0].volume_base, c[0].volume_quote), (Decimal(100), Decimal(1100)), key)

    def test_symbols_and_unlisted(self):
        self.assertEqual([P.EXCHANGES[k].symbol("SOL", "USDT") for k in ("gate", "binance", "kucoin", "okx")],
                         ["SOL_USDT", "SOLUSDT", "SOL-USDT", "SOL-USDT"])

        class NotFound:
            def get(self, *a, **k):
                return Resp({}, 400)

        now = datetime.now(timezone.utc)
        self.assertIsNone(P.candles("binance", "NOPE", "USDT", "1h", now - timedelta(hours=3), now, session=NotFound()))

    def test_tickers_normalise_percent(self):
        self.assertAlmostEqual(P.ticker("bybit", "SOL", "USDT", session=OneShot(
            {"retCode": 0, "result": {"list": [{"lastPrice": "70", "price24hPcnt": "-0.0716", "turnover24h": "5"}]}}))["change_pct"], -7.16)
        self.assertAlmostEqual(P.ticker("binance", "SOL", "USDT", session=OneShot(
            {"lastPrice": "70", "priceChangePercent": "-7.16", "quoteVolume": "5"}))["change_pct"], -7.16)


def fake_candles(n=120, step=3600):
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(seconds=step * n)
    out = []
    for i in range(n):
        p = Decimal(100 + (i % 10))  # a repeating range: 100..109
        out.append(CandleData(start + timedelta(seconds=step * i), p, p + 1, p - 1, p + Decimal("0.5"), Decimal(1), p, i < n - 1))
    return out


class ScannerTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_scan(self):
        with mock.patch("market.services.scanner.public.candles", return_value=fake_candles()):
            r = scanner.scan("binance", "sol", "usdt", 7, 1.0)
        self.assertEqual((r["base"], r["interval"], r["listed"]), ("SOL", "1h", True))
        self.assertEqual(len(r["bars"]), 120)
        self.assertGreater(len(r["hotspots"]), 0)
        self.assertEqual(r["stats"]["zones"], 2 * 119)  # the forming bar makes no zone
        self.assertIsNotNone(r["heatmap"])
        with mock.patch("market.services.scanner.public.candles", return_value=None):
            self.assertFalse(scanner.scan("mexc", "NOPE", "USDT", 7)["listed"])

    def test_page_and_monitoring_button(self):
        self.client.force_login(User.objects.create_user("s", password="not-a-real-pass-123"))
        where = [{"key": "gate", "name": "Gate.io", "listed": True, "last": "100", "change_pct": 1.0, "quote_volume": 5.0,
                  "vs_median_pct": 0.0, "volume_share_pct": 100.0, "error": ""}]
        with mock.patch("market.services.scanner.public.candles", return_value=fake_candles()), \
             mock.patch("market.services.scanner.where_it_trades", return_value=where):
            page = self.client.get(reverse("market:scanner"), {"symbol": "sol", "exchange": "binance", "days": "7"})
            self.assertContains(page, "Add for monitoring")
            self.assertContains(page, "Hotspots")
            Coin.objects.create(symbol="SOL", chain="SOL", gate_currency="SOL", verified_at=dj_tz.now())
            cache.clear()
            page = self.client.get(reverse("market:scanner"), {"symbol": "SOL", "exchange": "binance", "days": "7"})
            self.assertContains(page, "Already monitored")
        self.assertContains(self.client.get(reverse("market:scanner")), "Type a coin")


class Fixed:
    """Session returning one fixed payload for every request."""

    def __init__(self, data):
        self.data, self.urls = data, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.urls.append((url, params))
        return Resp(self.data)


class AllTickersTests(TestCase):
    def test_each_adapter_maps_and_filters_by_quote(self):
        cases = {
            "gate": [{"currency_pair": "SOL_USDT", "last": "100", "highest_bid": "99.9", "lowest_ask": "100", "high_24h": "110",
                      "low_24h": "90", "change_percentage": "2.5", "quote_volume": "1000"},
                     {"currency_pair": "SOL_BTC", "last": "1"}],
            "binance": [{"symbol": "SOLUSDT", "lastPrice": "100", "bidPrice": "99.9", "askPrice": "100", "highPrice": "110",
                         "lowPrice": "90", "priceChangePercent": "2.5", "quoteVolume": "1000"}, {"symbol": "SOLBTC"}],
            "mexc": [{"symbol": "SOLUSDT", "lastPrice": "100", "bidPrice": "99.9", "askPrice": "100", "highPrice": "110",
                      "lowPrice": "90", "priceChangePercent": "0.025", "quoteVolume": "1000"}],
            "bybit": {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "lastPrice": "100", "bid1Price": "99.9",
                      "ask1Price": "100", "highPrice24h": "110", "lowPrice24h": "90", "price24hPcnt": "0.025", "turnover24h": "1000"}]}},
            "kucoin": {"code": "200000", "data": {"ticker": [{"symbol": "SOL-USDT", "last": "100", "buy": "99.9", "sell": "100",
                       "high": "110", "low": "90", "changeRate": "0.025", "volValue": "1000"}, {"symbol": "SOL-BTC"}]}},
            "bitget": {"code": "00000", "data": [{"symbol": "SOLUSDT", "lastPr": "100", "bidPr": "99.9", "askPr": "100",
                       "high24h": "110", "low24h": "90", "open": "97.5609756", "quoteVolume": "1000"}]},
            "okx": {"code": "0", "data": [{"instId": "SOL-USDT", "last": "100", "bidPx": "99.9", "askPx": "100", "high24h": "110",
                    "low24h": "90", "open24h": "97.5609756", "volCcy24h": "1000"}, {"instId": "SOL-BTC", "last": "1"}]},
        }
        self.assertEqual(set(cases), set(P.EXCHANGES))
        for ex, data in cases.items():
            rows = P.all_tickers(ex, "usdt", session=Fixed(data))
            self.assertEqual(len(rows), 1, ex)
            r = rows[0]
            self.assertEqual((r["base"], r["last"], r["bid"], r["ask"], r["quote_volume"]), ("SOL", 100, 99.9, 100, 1000), ex)
            self.assertAlmostEqual(r["change_pct"], 2.5, places=4, msg=ex)

    def test_unreachable_raises(self):
        class Down:
            def get(self, *a, **k):
                raise P.requests.ConnectionError()
        with self.assertRaises(P.ExchangeError):
            P.all_tickers("okx", "USDT", session=Down())


class MultiExchangeScreenerTests(TestCase):
    def setUp(self):
        cache.clear()

    @staticmethod
    def row(base, last, vol, spread=0.1):
        return {"pair": base, "base": base, "last": last, "change_pct": 1.0, "volume": vol, "spread_pct": spread, "range_pct": 5.0}

    def test_combine_sums_volume_and_flags_price_gap(self):
        from market.services import screener
        out = {r["base"]: r for r in screener.combine({
            "gate": [self.row("SOL", 100, 1000), self.row("FAKE", 1.0, 50)],
            "binance": [self.row("SOL", 100.2, 9000, spread=0.01), self.row("FAKE", 2.0, 10)],
            "mexc": [self.row("ONLY", 3, 5)],
        })}
        sol = out["SOL"]
        self.assertEqual((sol["volume"], sol["lead"], sol["venue_count"], sol["spread_pct"]), (10000, "binance", 2, 0.01))
        self.assertEqual([v["key"] for v in sol["venues"]], ["binance", "gate"])
        self.assertAlmostEqual(sol["venues"][0]["share_pct"], 90)
        self.assertFalse(sol["gap_warn"])
        self.assertTrue(out["FAKE"]["gap_warn"])  # 100% apart: probably two coins sharing a ticker
        self.assertEqual(out["ONLY"]["venue_count"], 1)
        f = screener.Filters(min_volume=0, min_venues=2)
        self.assertEqual({r["base"] for r in screener.screen(list(out.values()), f)}, {"SOL", "FAKE"})

    def test_combined_skips_unreachable(self):
        from market.services import screener

        def fake(ex, quote):
            if ex == "okx":
                raise P.ExchangeError("unreachable (ConnectionError)")
            return [self.row("SOL", 100, 1000)] if ex in ("gate", "binance") else []
        with mock.patch.object(screener, "exchange_rows", side_effect=fake):
            rows, errors = screener.combined_rows("USDT")
        self.assertEqual(errors, {"OKX": "unreachable (ConnectionError)"})
        self.assertEqual(rows[0]["venue_count"], 2)

    def test_page_other_exchange_and_combined(self):
        from market.services import screener
        self.client.force_login(User.objects.create_user("m", password="not-a-real-pass-123"))
        gate = [self.row("SOL", 100, 5_000_000)]
        binance = [self.row("SOL", 100, 9_000_000), self.row("BNB", 600, 8_000_000)]
        with mock.patch.object(screener, "all_rows", return_value=gate), \
             mock.patch.object(screener.public, "all_tickers", side_effect=lambda ex, q: []), \
             mock.patch.object(screener, "rows_from_public", side_effect=lambda t, ex, q: binance if ex == "binance" else []):
            page = self.client.get(reverse("market:screener") + "?exchange=binance")
            self.assertContains(page, "Binance spot")
            self.assertContains(page, "not on Gate.io")  # BNB: Binance only
            self.assertContains(page, "exchange=binance")  # Scan opens the same exchange
            cache.clear()
            page = self.client.get(reverse("market:screener") + "?exchange=all&min_venues=2")
        self.assertContains(page, "All exchanges (combined)")
        self.assertContains(page, "Binance 64%")
        self.assertContains(page, "Add coin")
        self.assertNotContains(page, "<b>BNB</b>")
