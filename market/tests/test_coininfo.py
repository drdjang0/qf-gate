"""Auto-fill of coin details from the symbol (Gate.io identity + CoinGecko enrichment)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.models import Coin
from market.services import coininfo

USDT_CHAINS = [{"name": "TRX", "addr": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
               {"name": "ETH", "addr": "0xdAC17F958D2ee523a2206206994597C13D831ec7"},
               {"name": "TON", "addr": "EQCx", "withdraw_disabled": True}]


class FakeGate:
    def _get(self, path, params=None, retries=4):
        return {"/spot/currencies/SOL": {"currency": "SOL", "name": "Solana", "chain": "SOL", "chains": [{"name": "SOL", "addr": ""}]},
                "/spot/currencies/USDT": {"currency": "USDT", "name": "Tether", "chain": "ETH", "chains": USDT_CHAINS}}.get(path)

    def pair_status(self, pair):
        return "tradable" if pair in ("SOL_USDT",) else None


class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.ok = data, code, code == 200

    def json(self):
        return self._d


class FakeCG:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if url.endswith("/search"):
            return Resp({"coins": [{"id": "solana-name-service", "symbol": "SNS", "market_cap_rank": 9},
                                   {"id": "wrapped-sol", "symbol": "SOL", "market_cap_rank": 500},
                                   {"id": "solana", "symbol": "SOL", "market_cap_rank": 7}]})
        if url.endswith("/coins/solana"):
            return Resp({"id": "solana", "name": "Solana", "image": {"small": "https://img/sol.png"},
                         "tickers": [{"market": {"name": "Binance"}}, {"market": {"name": "Gate"}}, {"market": {"name": "Binance"}}]})
        if "/contract/0xdac17f958d2ee523a2206206994597c13d831ec7" in url:
            return Resp({"id": "tether", "image": {"small": "https://img/usdt.png"}, "platforms": {"tron": "TR7N"},
                         "tickers": [{"market": {"name": "OKX"}}]})
        return Resp({"error": "not found"}, 404)


class CoinInfoTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_native_coin(self):
        info = coininfo.autofill("sol", client=FakeGate(), session=FakeCG())
        g, cg = info["gate"], info["coingecko"]
        self.assertEqual((g["chain"], g["contract"], g["native"], g["pair_status"]), ("SOL", "", True, "tradable"))
        self.assertEqual(cg["id"], "solana")  # exact symbol, best rank: not SNS, not wrapped SOL
        self.assertEqual(cg["exchanges"], ["Binance", "Gate"])
        self.assertEqual(cg["image"], "https://img/sol.png")

    def test_token_uses_gate_primary_chain_and_contract(self):
        fake = FakeCG()
        info = coininfo.autofill("USDT", client=FakeGate(), session=fake)
        g = info["gate"]
        self.assertEqual((g["chain"], g["contract"]), ("ETH", "0xdAC17F958D2ee523a2206206994597C13D831ec7"))
        self.assertEqual([c["chain"] for c in g["chains"]], ["ETH", "TRX", "TON"])  # primary, working, disabled
        self.assertTrue(any("/coins/ethereum/contract/" in u for u in fake.calls))  # matched by contract
        self.assertEqual(info["coingecko"]["id"], "tether")

    def test_unknown_and_coingecko_down(self):
        self.assertFalse(coininfo.autofill("NOPE", client=FakeGate(), session=FakeCG())["found"])

        class Down:
            def get(self, *a, **k):
                return Resp({}, 429)

        info = coininfo.autofill("SOL", client=FakeGate(), session=Down())
        self.assertTrue(info["found"])
        self.assertIsNone(info["coingecko"])  # still usable without CoinGecko

    def test_enrich_and_views(self):
        coin = Coin.objects.create(symbol="SOL", chain="SOL", gate_currency="SOL", verified_at=dj_tz.now())
        self.assertTrue(coininfo.enrich(coin, client=FakeGate(), session=FakeCG()))
        coin.refresh_from_db()
        self.assertEqual((coin.coingecko_id, coin.image_url, coin.exchanges), ("solana", "https://img/sol.png", ["Binance", "Gate"]))
        self.client.force_login(User.objects.create_user("a", password="not-a-real-pass-123"))
        with mock.patch("market.services.coininfo.autofill", return_value={"found": True, "symbol": "SOL", "gate": {}}):
            d = self.client.get(reverse("market:coin_autofill"), {"symbol": "sol"}).json()
        self.assertEqual(d["existing"], reverse("market:coin_detail", args=[coin.pk]))
        self.assertEqual(self.client.get(reverse("market:coin_autofill")).status_code, 400)
        page = self.client.get(reverse("market:coin_create") + "?symbol=SOL&gate_currency=SOL&quote=USDT")
        self.assertContains(page, "Fetch details")
        self.assertContains(self.client.get(reverse("market:coin_detail", args=[coin.pk])), "Refresh details")
