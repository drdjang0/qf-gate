"""Phase 10: users and their own encrypted keys, per-user settings, live order safety, live cascades, fill listener."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import timedelta
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from accounts import crypto
from accounts.keys import gate_credentials, save_key
from accounts.models import ExchangeKey
from cascades import engine, ladder
from cascades.models import Cascade, CascadeLevel
from ledger import recorder
from ledger.models import Transaction
from market.models import Coin
from trading import bots as bots_mod
from trading.brokers import GateBroker, OrderError, live_allowed
from trading.models import Bot, StrategyConfig, TradingControl

FKEY = Fernet.generate_key().decode()
LIVE = {**settings.TRADING, "LIVE_TRADING": "true"}


class FakeClient:
    def get_ticker(self, pair):
        return {"last": "100"}

    def pair_info(self, pair):
        return {"price_precision": 2, "amount_precision": 4, "min_base_amount": None, "min_quote_amount": 1.0}


class FakeGate(GateBroker):
    """Gate.io in memory: limit orders rest until the test fills or cancels them."""

    def __init__(self):  # no keys, no network
        self.client = FakeClient()
        self.orders: dict[str, dict] = {}
        self.next = 100
        self.stops: dict[str, dict] = {}

    def limit(self, pair, side, amount_base, price, text):
        amount = int(amount_base * 10_000) / 10_000
        self.next += 1
        oid = str(self.next)
        self.orders[oid] = {"id": oid, "text": text, "side": side, "amount": str(amount), "price": str(price),
                            "status": "open", "filled_amount": "0", "avg_deal_price": "0", "fee": "0", "fee_currency": ""}
        return dict(self.orders[oid])

    def get_order(self, pair, order_id):
        return dict(self.orders[order_id])

    def find_order(self, pair, text, since):
        return next((dict(o) for o in self.orders.values() if o["text"] == text), None)

    def cancel_order(self, pair, order_id):
        o = self.orders[order_id]
        if o["status"] == "open":
            o["status"] = "cancelled"
        return dict(o)

    def fill(self, oid, fee, fee_currency):
        o = self.orders[oid]
        o.update(status="closed", filled_amount=o["amount"], avg_deal_price=o["price"], fee=str(fee),
                 fee_currency=fee_currency)


@override_settings(FIELD_ENCRYPTION_KEY=FKEY)
class KeyTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("ann", password="not-a-real-pass-123")
        self.client.force_login(self.u)

    def test_keys_are_encrypted_and_never_shown(self):
        k = save_key(self.u, "  mykey1234ABCD ", "s3cret-value-xyz")
        self.assertNotIn("mykey1234ABCD", k.api_key_enc)
        self.assertNotIn("s3cret", k.secret_enc)
        self.assertEqual(k.credentials(), ("mykey1234ABCD", "s3cret-value-xyz"))
        self.assertEqual(k.key_last4, "ABCD")
        page = self.client.get(reverse("accounts:keys")).content.decode()
        self.assertIn("ABCD", page)
        self.assertNotIn("s3cret", page)
        self.assertNotIn("mykey1234", page)
        # a second key replaces the first
        save_key(self.u, "otherkeyWXYZ", "x")
        self.assertEqual(ExchangeKey.objects.filter(owner=self.u, is_active=True).get().key_last4, "WXYZ")
        self.assertEqual(gate_credentials(self.u), ("otherkeyWXYZ", "x"))

    def test_wrong_encryption_key_is_reported(self):
        save_key(self.u, "k", "s")
        with override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(crypto.KeyStoreError):
                gate_credentials(self.u)

    def test_key_page_tests_before_saving(self):
        with mock.patch("accounts.views.test_gate_key", return_value={"spot_read": False, "wallet_read": False,
                                                                      "error": "INVALID_KEY"}):
            r = self.client.post(reverse("accounts:keys"), {"api_key": "k1", "api_secret": "s1", "test": "on"}, follow=True)
        self.assertContains(r, "refused the key")
        self.assertFalse(ExchangeKey.objects.exists())
        with mock.patch("accounts.views.test_gate_key", return_value={"spot_read": True, "wallet_read": True, "error": ""}):
            self.client.post(reverse("accounts:keys"), {"api_key": "key-9999", "api_secret": "s1", "test": "on"})
        k = ExchangeKey.objects.get()
        self.assertTrue(k.permissions["spot_read"])
        self.client.post(reverse("accounts:key_action", args=[k.pk, "delete"]))
        self.assertFalse(ExchangeKey.objects.exists())

    @override_settings(FIELD_ENCRYPTION_KEY="")
    def test_no_server_key_no_saving(self):
        r = self.client.get(reverse("accounts:keys"))
        self.assertContains(r, "make_encryption_key")
        with self.assertRaises(crypto.KeyStoreError):
            save_key(self.u, "k", "s")


@override_settings(FIELD_ENCRYPTION_KEY=FKEY)
class IsolationAndSettingsTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("a", password="not-a-real-pass-123")
        self.b = User.objects.create_user("b", password="not-a-real-pass-123")
        self.coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.cfg = StrategyConfig.objects.create(owner=self.a, name="Alpha secret", strategy_key="pdh_breakout",
                                                 coin=self.coin, interval="1h")

    def test_each_user_sees_only_their_own(self):
        self.client.force_login(self.b)
        self.assertEqual(self.client.get(reverse("trading:strategy_detail", args=[self.cfg.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("trading:strategy_list")), "Alpha secret")
        c = Cascade.objects.create(owner=self.a, coin=self.coin, name="A ladder")
        self.assertEqual(self.client.get(reverse("cascades:detail", args=[c.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("cascades:index")), "A ladder")
        acc = recorder.account(recorder.GATE, self.a)
        Transaction.objects.create(account=acc, base="ETH", side="buy", qty=1, price=1, executed_at=dj_tz.now(),
                                   source_key="x")
        csv = self.client.get(reverse("ledger:transactions"), {"format": "csv", "scope": "real"}).content.decode()
        self.assertEqual(len(csv.strip().splitlines()), 1)  # header only
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(reverse("trading:strategy_detail", args=[self.cfg.pk])).status_code, 200)
        csv = self.client.get(reverse("ledger:transactions"), {"format": "csv", "scope": "real"}).content.decode()
        self.assertEqual(len(csv.strip().splitlines()), 2)

    def test_users_page_is_for_admins(self):
        self.client.force_login(self.b)
        self.assertEqual(self.client.get(reverse("accounts:users")).status_code, 302)
        boss = User.objects.create_user("boss", password="not-a-real-pass-123", is_staff=True)
        self.client.force_login(boss)
        r = self.client.post(reverse("accounts:users"), {"username": "newbie", "password1": "Long-enough-pass-987",
                                                         "password2": "Long-enough-pass-987"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.filter(username="newbie", is_staff=False).exists())
        newbie = User.objects.get(username="newbie")
        self.client.post(reverse("accounts:user_action", args=[newbie.pk, "toggle_active"]))
        newbie.refresh_from_db()
        self.assertFalse(newbie.is_active)
        self.assertTrue(TradingControl.for_user(newbie).halted)

    @override_settings(TRADING=LIVE)
    def test_live_switch_needs_key_and_confirmation(self):
        self.client.force_login(self.a)
        self.client.post(reverse("accounts:settings"), {"action": "mode", "mode": "live", "confirm": "LIVE"})
        self.assertFalse(TradingControl.for_user(self.a).live_enabled)  # no key yet
        self.assertIn("No Gate.io API key", " ".join(live_allowed(self.a)[1]))
        save_key(self.a, "akey", "asecret")
        self.client.post(reverse("accounts:settings"), {"action": "mode", "mode": "live", "confirm": "nope"})
        self.assertFalse(TradingControl.for_user(self.a).live_enabled)
        self.client.post(reverse("accounts:settings"), {"action": "mode", "mode": "live", "confirm": "LIVE"})
        self.assertTrue(TradingControl.for_user(self.a).live_enabled)
        self.assertEqual(live_allowed(self.a), (True, []))
        self.assertFalse(live_allowed(self.b)[0])  # b has no key and hasn't switched on

    def test_strategies_off_checks_off_and_optional_limits(self):
        self.client.force_login(self.a)
        self.client.post(reverse("accounts:settings"), {"action": "save", "min_dry_run_days": "7",
                                                        "max_daily_loss_pct": "", "max_exposure": "",
                                                        "max_orders_per_hour": ""})
        c = TradingControl.for_user(self.a)
        self.assertEqual((c.strategies_enabled, c.require_checks, c.max_daily_loss_pct, c.max_orders_per_hour),
                         (False, False, None, 0))
        page = self.client.get(reverse("cascades:index")).content.decode()
        self.assertNotIn(reverse("trading:strategy_list"), page)
        with self.assertRaises(ValueError):
            bots_mod.start_bot(self.cfg)
        items = bots_mod.promotion_status(self.cfg)["items"]
        self.assertTrue(any("switched off in your settings" in i["text"] for i in items))
        self.assertFalse(any("Dry-run for at least" in i["text"] for i in items))


class BrokerSafetyTests(TestCase):
    def test_buy_fee_taken_in_the_coin_is_not_sold_later(self):
        g = FakeGate()
        f = g.fill_from({"id": "1", "side": "buy", "filled_amount": "0.1", "avg_deal_price": "1000", "fee": "0.0002",
                         "fee_currency": "BTC"}, "BTC_USDT")
        self.assertAlmostEqual(f.qty, 0.0998)
        self.assertAlmostEqual(f.fee, 0.2)
        s = g.fill_from({"id": "2", "side": "sell", "filled_amount": "0.0998", "avg_deal_price": "1100", "fee": "0.21",
                         "fee_currency": "USDT"}, "BTC_USDT")
        self.assertAlmostEqual(s.qty, 0.0998)
        self.assertAlmostEqual(s.fee, 0.21)
        self.assertIsNone(g.fill_from({"side": "buy", "filled_amount": "0", "avg_deal_price": "0"}, "BTC_USDT"))

    def test_minimum_order_checked_before_sending(self):
        with self.assertRaises(OrderError):
            FakeGate()._check_minimum({"min_quote_amount": 3.0}, 0.001, 1.0)
        FakeGate()._check_minimum({"min_quote_amount": None, "min_base_amount": None}, 0.001, 1.0)


class RecoveryTests(TestCase):
    def setUp(self):
        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.cfg = StrategyConfig.objects.create(name="r", strategy_key="pdh_breakout", coin=coin, interval="1h")

    def _bot(self, mode, **state):
        return Bot.objects.create(config=self.cfg, mode=mode, strategy_key="pdh_breakout", interval="1h",
                                  state={"engine": {}, "pos": None, "cash": 1000.0, **state})

    def test_dry_run_slot_freed(self):
        b = self._bot("dry_run", busy=True, busy_since=time.time() - 600)
        self.assertIsNone(bots_mod.recover_stuck(b))
        self.assertFalse(Bot.objects.get(pk=b.pk).state["busy"])

    def test_live_order_found_by_client_id_and_booked(self):
        g = FakeGate()
        o = g.limit("BTC_USDT", "buy", 0.1, 100, "t-b1x123")
        g.fill(o["id"], 0.0002, "BTC")
        b = self._bot("live", busy=True, busy_since=time.time() - 600, client_id="t-b1x123", busy_side="buy",
                      busy_reason="signal", busy_ref=100.0, busy_stop=95.0)
        order = bots_mod.recover_stuck(b, broker=g)
        self.assertEqual(order.status, "filled")
        st = Bot.objects.get(pk=b.pk).state
        self.assertFalse(st["busy"])
        self.assertAlmostEqual(st["pos"]["qty"], 0.0998)
        self.assertEqual(st["pos"]["stop"], 95.0)
        self.assertNotIn("client_id", st)

    def test_recent_busy_slot_is_left_alone(self):
        b = self._bot("live", busy=True, busy_since=time.time())
        self.assertIsNone(bots_mod.recover_stuck(b, broker=FakeGate()))
        self.assertTrue(Bot.objects.get(pk=b.pk).state["busy"])


@override_settings(FIELD_ENCRYPTION_KEY=FKEY, TRADING=LIVE)
class LiveCascadeTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("cas", password="not-a-real-pass-123")
        save_key(self.u, "key", "secret")
        c = TradingControl.for_user(self.u)
        c.live_enabled = True
        c.save()
        self.coin = Coin.objects.create(symbol="ETH", chain="ETH", gate_currency="ETH", verified_at=dj_tz.now())
        self.g = FakeGate()
        p = mock.patch("cascades.live.broker_for", return_value=self.g)
        p.start()
        self.addCleanup(p.stop)
        self.levels = ladder.compute_levels(100, 10, 10, 0, 3, 5, 0)

    def lv(self, c, i):
        return CascadeLevel.objects.get(cascade=c, index=i)

    def test_real_orders_follow_the_ladder(self):
        c = engine.create(self.coin, "percent", self.levels, price=101, owner=self.u, mode="live")
        l1 = self.lv(c, 1)
        self.assertEqual(l1.status, "placed")
        self.assertTrue(l1.buy_order_id)
        self.assertEqual(self.g.orders[l1.buy_order_id]["amount"], "0.1")
        self.assertEqual(self.g.orders[l1.buy_order_id]["side"], "buy")
        # no price-based fill in live mode, even when the price trades through
        engine.step(c.pk, 95)
        self.assertEqual(self.lv(c, 1).status, "placed")
        # Gate.io fills it (fee taken in ETH): booked with the net amount, take-profit and level 2 sent at once
        self.g.fill(l1.buy_order_id, 0.0002, "ETH")
        engine.step(c.pk, 99)
        l1, l2 = self.lv(c, 1), self.lv(c, 2)
        self.assertEqual(l1.status, "filled")
        self.assertAlmostEqual(l1.qty, 0.0998)
        self.assertAlmostEqual(l1.buy_fee, 0.02)
        self.assertTrue(l1.sell_order_id)
        self.assertEqual(self.g.orders[l1.sell_order_id]["amount"], "0.0998")
        self.assertEqual(self.g.orders[l1.sell_order_id]["price"], "105.0")
        self.assertEqual(l2.status, "placed")
        self.assertTrue(l2.buy_order_id)
        # the take-profit fills
        self.g.fill(l1.sell_order_id, 0.0998 * 105 * 0.002, "USDT")
        engine.step(c.pk, 104)
        l1 = self.lv(c, 1)
        self.assertEqual(l1.sell_status, "filled")
        self.assertAlmostEqual(l1.pnl, 0.0998 * 105 * 0.998 - 10, places=6)
        # hold: the resting buy is cancelled on Gate.io
        l2_order = self.lv(c, 2).buy_order_id
        engine.hold(Cascade.objects.get(pk=c.pk))
        self.assertEqual(self.g.orders[l2_order]["status"], "cancelled")
        self.assertEqual(self.lv(c, 2).buy_order_id, "")
        # the ledger leaves live cascades to Gate.io's own trades (no double counting)
        recorder.record_internal()
        self.assertFalse(Transaction.objects.filter(route="cascade", source_key__startswith="cascade:").exists())

    def test_edit_resends_and_external_cancel_rechecks(self):
        c = engine.create(self.coin, "percent", self.levels, price=101, owner=self.u, mode="live")
        first = self.lv(c, 1).buy_order_id
        engine.edit_level(self.lv(c, 1), target_price=99)
        self.assertEqual(self.g.orders[first]["status"], "cancelled")
        engine.step(c.pk, 101)
        second = self.lv(c, 1).buy_order_id
        self.assertNotEqual(first, second)
        self.assertEqual(self.g.orders[second]["price"], "99.0")
        self.g.orders[second]["status"] = "cancelled"  # someone cancelled it on Gate.io
        engine.step(c.pk, 101)
        self.assertTrue(self.lv(c, 1).buy_order_id)
        self.assertNotEqual(self.lv(c, 1).buy_order_id, second)

    def test_live_needs_permission(self):
        TradingControl.objects.filter(owner=self.u).update(live_enabled=False)
        with self.assertRaises(ValueError):
            engine.create(self.coin, "percent", self.levels, price=101, owner=self.u, mode="live")


@override_settings(FIELD_ENCRYPTION_KEY=FKEY)
class ListenerTests(TestCase):
    def test_signature_and_subscribe_message(self):
        from accounts.management.commands.stream_account import subscribe_msg

        m = subscribe_msg("spot.orders", ["BTC_USDT"], "KEY", "SECRET", ts=1611541000)
        want = hmac.new(b"SECRET", b"channel=spot.orders&event=subscribe&time=1611541000", hashlib.sha512).hexdigest()
        self.assertEqual(m["auth"], {"method": "api_key", "KEY": "KEY", "SIGN": want})
        self.assertEqual((m["event"], m["payload"]), ("subscribe", ["BTC_USDT"]))

    def test_trade_event_goes_into_the_owners_ledger_once(self):
        from accounts.management.commands.stream_account import handle_trade

        u = User.objects.create_user("t", password="not-a-real-pass-123")
        coin = Coin.objects.create(symbol="ETH", chain="ETH", gate_currency="ETH", verified_at=dj_tz.now())
        c = Cascade.objects.create(owner=u, coin=coin, mode="live", name="L")
        lv = CascadeLevel.objects.create(cascade=c, index=2, target_price=100, amount_usdt=10, take_profit_pct=5)
        ts = int((dj_tz.now() - timedelta(seconds=5)).timestamp())
        item = {"id": 777, "create_time_ms": f"{ts * 1000}.5", "currency_pair": "ETH_USDT", "side": "buy",
                "amount": "0.1", "price": "100", "order_id": "55", "fee": "0.0002", "fee_currency": "ETH",
                "point_fee": "0", "gt_fee": "0", "text": f"t-c{lv.pk}b123", "role": "maker"}
        with mock.patch("ledger.gate_sync.price_at", return_value=None):
            self.assertEqual(handle_trade(u.pk, item), 1)
            self.assertEqual(handle_trade(u.pk, item), 0)
        t = Transaction.objects.get()
        self.assertEqual((t.account.owner, t.route, t.source_key), (u, "cascade", "gate:spot:trade:777"))
        self.assertIn(f"Cascade #{c.pk}", t.source)
        self.assertIn("L2 buy", t.source)
