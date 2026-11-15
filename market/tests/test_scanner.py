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
