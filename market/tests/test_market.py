from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from market.exchanges.gate import GateClient, parse_rest_candle, parse_ws_candle
from market.models import Candle, Coin, DailyLevel, normalize_contract
from market.services import sync as sync_mod
from market.services.archive import candles_from_trades
from market.services.levels import compute_levels, levels_with_previous
from market.services.sync import sync_coin, upsert_candles
from market.services.verify import verify_identity
from market.templatetags.market_extras import price

from .fakes import PEPE_ETH, FakeClient, synth_candle, utc


class ParsingTests(TestCase):
    def test_rest_row(self):
        c = parse_rest_candle(["1794162360", "148776.3", "75920.4", "75933.1", "75920.3", "75933.1", "1.959486", "true"])
        self.assertEqual(c.open_time, utc(2026, 11, 8, 18, 26))
        self.assertEqual((c.open, c.high, c.low, c.close), tuple(map(Decimal, ["75933.1", "75933.1", "75920.3", "75920.4"])))
        self.assertTrue(c.is_closed)

    def test_ws_update(self):
        interval, pair, c = parse_ws_candle(
            {"t": "1794162660", "v": "138456.9", "c": "75952.7", "h": "75952.8", "l": "75949.1",
             "o": "75949.3", "n": "1m_BTC_USDT", "a": "1.82", "w": False}
        )
        self.assertEqual((interval, pair), ("1m", "BTC_USDT"))
        self.assertFalse(c.is_closed)
        self.assertEqual(c.high, Decimal("75952.8"))

    def test_earliest_available_respects_10000_point_window(self):
        client = GateClient(base_url="http://x")
        now = utc(2026, 11, 8, 18, 30, 15)
        self.assertEqual(client.earliest_available("1m", now), utc(2026, 11, 8, 18, 30) - timedelta(minutes=9995))

    def test_get_candles_chunks_by_1000(self):
        client = GateClient(base_url="http://x")
        now = utc(2026, 11, 8, 12, 0)
        calls = []

        def fake_get(path, params=None, retries=4):
            calls.append(params)
            t, end = params["from"], params["to"]
            return [[str(x), "1", "1", "1", "1", "1", "1", "true"] for x in range(t, end + 1, 60)]

        with mock.patch.object(client, "_get", side_effect=fake_get), \
             mock.patch.object(client, "earliest_available", return_value=now - timedelta(days=6)), \
             mock.patch("market.exchanges.gate.time.sleep"):
            out = client.get_candles("BTC_USDT", "1m", now - timedelta(minutes=2500), now)
        self.assertEqual(len(out), 2501)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all((p["to"] - p["from"]) // 60 + 1 <= 1000 for p in calls))

    def test_price_filter(self):
        self.assertEqual(price(Decimal("78000.000")), "78000")
        self.assertEqual(price(Decimal("0.0000012300")), "0.00000123")
        self.assertEqual(price(None), "—")


class IdentityTests(TestCase):
    def setUp(self):
        self.client_ = FakeClient(now=utc(2026, 11, 8, 12))

    def verify(self, **kw):
        base = {"symbol": "", "chain": "ETH", "contract_address": "", "gate_currency": "", "quote": "USDT"}
        return verify_identity(self.client_, **{**base, **kw})

    def test_normalize_contract(self):
        self.assertEqual(normalize_contract(" 0xABCdef "), "0xabcdef")
        sol = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.assertEqual(normalize_contract(sol), sol)  # base58 is case-sensitive

    def test_contract_resolves_gate_code_case_insensitively(self):
        res = self.verify(symbol="PEPE", contract_address=PEPE_ETH.lower())
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.gate_currency, "PEPE")

    def test_symbol_confusion_is_caught(self):
        # ticker typed as PEPE2 but the real PEPE contract pasted -> resolves to PEPE, with a warning
        res = self.verify(symbol="PEPE2", contract_address=PEPE_ETH)
        self.assertTrue(res.ok)
        self.assertEqual(res.gate_currency, "PEPE")
        self.assertTrue(any("differs" in w for w in res.warnings))
        # an explicit Gate code that does not own the contract is rejected
        res = self.verify(symbol="PEPE", contract_address=PEPE_ETH, gate_currency="PEPE2")
        self.assertFalse(res.ok)

    def test_wrong_chain_suggests_correct_one(self):
        res = self.verify(symbol="PEPE", chain="BSC", contract_address=PEPE_ETH)
        self.assertFalse(res.ok)
        self.assertIn("PEPE on ETH", res.errors[0])

    def test_unknown_contract(self):
        self.assertFalse(self.verify(symbol="X", contract_address="0x" + "9" * 40).ok)

    def test_native_coin(self):
        self.assertTrue(self.verify(symbol="BTC", chain="BTC").ok)
        self.assertFalse(self.verify(symbol="BTC", chain="ETH").ok)
        res = self.verify(symbol="BTC", chain="BSC")  # BTC on BSC is a token -> needs its contract
        self.assertFalse(res.ok)
        self.assertIn("contract address", res.errors[0])

    def test_pair_status(self):
        res = self.verify(symbol="PEPE2", chain="BSC", contract_address="0x1111111111111111111111111111111111111111")
        self.assertTrue(res.ok)
        self.assertTrue(any("untradable" in w for w in res.warnings))

    def test_base_equal_to_quote_rejected(self):
        from .fakes import USDT_ETH

        res = self.verify(symbol="USDT", contract_address=USDT_ETH, quote="USDT")
        self.assertFalse(res.ok)
        self.assertIn("quote currency itself", res.errors[0])

    def test_unique_identity_constraints(self):
        Coin.objects.create(symbol="PEPE", chain="ETH", contract_address=PEPE_ETH, gate_currency="PEPE")
        with self.assertRaises(IntegrityError):
            Coin.objects.create(symbol="PEPEX", chain="ETH", contract_address=PEPE_ETH.lower(), gate_currency="PEPEX")


class ArchiveTests(TestCase):
    def test_trades_to_minutes(self):
        lines = [
            b"1790812800.5,1,100,1,2\n",
            b"1790812801.0,2,105,2,1\n",
            b"1790812859.9,3,99,1,1\n",
            b"1790812800.1,0,101,1,1\n",  # out of order: the earliest trade is the open
            b"1790812990.0,4,110,1,2\n",  # 3 minutes later -> two flat gap candles in between
            b"garbage\n",
            b"1790899999.0,5,1,1,1\n",   # outside the range
        ]
        start = utc(2026, 10, 1)
        out = candles_from_trades(lines, start, start + timedelta(hours=1))
        self.assertEqual([c.open_time.minute for c in out], [0, 1, 2, 3])
        first = out[0]
        self.assertEqual((first.open, first.high, first.low, first.close), tuple(map(Decimal, ["101", "105", "99", "99"])))
        self.assertEqual(first.volume_base, Decimal(5))
        self.assertEqual(out[1].volume_base, 0)
        self.assertEqual(out[1].high, Decimal(99))
        self.assertEqual(out[3].close, Decimal(110))


class LevelTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC")

    def _fill(self, interval, start, end, step):
        t = int(start.timestamp())
        candles = []
        while t < int(end.timestamp()):
            candles.append(synth_candle(t, step))
            t += step
        upsert_candles(self.coin, interval, candles, Candle.Source.REST)

    def test_1m_day_exact_time(self):
        d = utc(2026, 10, 10)
        self._fill("1m", d, d + timedelta(days=1), 60)
        compute_levels(self.coin, d.date(), d.date(), now=d + timedelta(days=2))
        lv = DailyLevel.objects.get(coin=self.coin, date=d.date())
        self.assertEqual((lv.high, lv.low), (Decimal(1720), Decimal(1000)))
        self.assertEqual(lv.high_time, d)
        self.assertEqual(lv.low_time, d + timedelta(hours=12))
        self.assertEqual(lv.time_precision, "1m")
        self.assertTrue(lv.is_complete)
        self.assertEqual(lv.candle_count, 1440)

    def test_fallback_5m_refined_with_partial_1m(self):
        d = utc(2026, 11, 1)
        self._fill("5m", d, d + timedelta(days=1), 300)
        self._fill("1m", d + timedelta(hours=11), d + timedelta(days=1), 60)  # 1m only from 11:00
        compute_levels(self.coin, d.date(), d.date(), now=d + timedelta(days=2))
        lv = DailyLevel.objects.get(coin=self.coin, date=d.date())
        self.assertEqual((lv.high, lv.low), (Decimal(1720), Decimal(1000)))
        self.assertEqual(lv.low_time, d + timedelta(hours=12))  # refined to the minute from 1m
        self.assertEqual(lv.high_time, d)  # 00:00 has no 1m -> 5m precision
        self.assertEqual(lv.time_precision, "5m")

    def test_today_is_partial_and_cross_check(self):
        d = utc(2026, 11, 8)
        now = d + timedelta(hours=6, seconds=30)
        self._fill("1m", d, now, 60)
        compute_levels(self.coin, d.date(), d.date(), now=now)
        self.assertFalse(DailyLevel.objects.get(coin=self.coin, date=d.date()).is_complete)

        prev = d - timedelta(days=1)
        self._fill("1m", prev, d, 60)
        upsert_candles(self.coin, "1d", [synth_candle(int(prev.timestamp()), 86400)], Candle.Source.REST)
        compute_levels(self.coin, prev.date(), prev.date(), now=now)
        self.assertEqual(DailyLevel.objects.get(coin=self.coin, date=prev.date()).cross_check, "")

    def test_timezone_day_boundary(self):
        d = utc(2026, 10, 9, 19)  # 2026-10-10 00:00 in Asia/Karachi (UTC+5)
        self._fill("1m", d, d + timedelta(days=1), 60)
        day = d.date() + timedelta(days=1)
        compute_levels(self.coin, day, day, tz_name="Asia/Karachi", now=d + timedelta(days=3))
        lv = DailyLevel.objects.get(coin=self.coin, timezone_name="Asia/Karachi")
        self.assertEqual(lv.candle_count, 1440)
        self.assertEqual(lv.time_precision, "1m")

    def test_pdh_pdl(self):
        for i in range(3):
            d = utc(2026, 10, 10 + i)
            self._fill("1m", d, d + timedelta(days=1), 60)
        compute_levels(self.coin, utc(2026, 10, 10).date(), utc(2026, 10, 12).date(), now=utc(2026, 10, 20))
        rows = levels_with_previous(self.coin)
        self.assertEqual(rows[0]["level"].date.day, 12)
        self.assertEqual(rows[0]["pdh"], rows[1]["level"].high)
        self.assertIsNone(rows[-1]["pdh"])


class SyncTests(TestCase):
    def setUp(self):
        self.now = utc(2026, 11, 8, 18, 30, 20)
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC")
        self.fake = FakeClient(now=self.now)

    def test_full_sync_uses_archive_then_fallbacks_and_is_idempotent(self):
        def fake_archive(pair, y, m, start, end):
            if (y, m) >= (2026, 11):
                return None  # current month not published yet
            t, out = int(start.timestamp()), []
            while t < int(end.timestamp()):
                out.append(synth_candle(t, 60))
                t += 60
            return out

        with mock.patch.object(sync_mod.archive, "archive_candles", side_effect=fake_archive):
            stats = sync_coin(self.coin, days=60, client=self.fake, now=self.now)
            n_1m = Candle.objects.filter(coin=self.coin, interval="1m").count()
            sync_coin(self.coin, days=60, client=self.fake, now=self.now)  # re-run
        self.assertEqual(Candle.objects.filter(coin=self.coin, interval="1m").count(), n_1m)
        self.assertGreater(stats["archive_1m"], 0)
        self.assertIn("5m", stats["fallback"])  # Nov 1 is before the 1m REST window

        levels = DailyLevel.objects.filter(coin=self.coin)
        self.assertEqual(levels.count(), 61)
        self.assertTrue(all(lv.high == 1720 and lv.low == 1000 for lv in levels.exclude(date=self.now.date())))
        self.assertTrue(all(lv.cross_check == "" for lv in levels))
        nov1 = levels.get(date=utc(2026, 11, 1).date())
        self.assertEqual(nov1.time_precision, "5m")
        self.assertEqual(nov1.low_time, utc(2026, 11, 1, 12))
        self.assertEqual(levels.get(date=utc(2026, 10, 15).date()).time_precision, "1m")

        # the second run re-requests 1m only from the last closed candle
        one_min_calls = [c for c in self.fake.calls if c[2] == "1m"]
        self.assertGreaterEqual(one_min_calls[-1][3], self.now - timedelta(minutes=2))


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.fake = FakeClient(now=utc(2026, 11, 8, 12))
        p = mock.patch("market.forms.get_client", return_value=self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_login_required(self):
        r = self.client.get(reverse("market:coin_list"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])

    def test_crud_with_contract_verification(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("market:coin_create"), {
            "symbol": "pepe", "chain": "eth", "contract_address": PEPE_ETH, "quote": "USDT", "is_active": "on",
        })
        self.assertEqual(r.status_code, 302, r.content[:2000])
        coin = Coin.objects.get()
        self.assertEqual((coin.symbol, coin.chain, coin.gate_currency, coin.pair), ("PEPE", "ETH", "PEPE", "PEPE_USDT"))
        self.assertIsNotNone(coin.verified_at)
        self.assertEqual(coin.name, "Pepe")

        r = self.client.post(reverse("market:coin_update", args=[coin.pk]), {
            "symbol": "PEPE", "chain": "BSC", "contract_address": PEPE_ETH, "quote": "USDT",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "different chain code")

        self.assertEqual(self.client.get(reverse("market:coin_detail", args=[coin.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("market:coin_list")).status_code, 200)
        r = self.client.post(reverse("market:coin_delete", args=[coin.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Coin.objects.exists())

    def test_export_xlsx(self):
        self.client.force_login(self.user)
        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC")
        d = utc(2026, 10, 10)
        DailyLevel.objects.create(
            coin=coin, date=d.date(), open=1, close=1, high=Decimal("78406"), high_time=d, low=Decimal("77490"),
            low_time=d, time_precision="1m", candle_count=1440, is_complete=True,
        )
        r = self.client.get(reverse("market:export_levels") + f"?coin={coin.pk}")
        self.assertEqual(r.status_code, 200)
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertEqual(ws["B3"].value, 78406)
        self.assertEqual(ws["D3"].value, 77490)
