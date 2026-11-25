"""Phase 4: brokers, bots (dry-run), risk limits, kill switch, alerts, Telegram, control pages."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from market.models import Candle, Coin
from trading import bots, brokers, notify, telegram
from trading.brokers import DryRunBroker, Fill, GateBroker, OrderError
from trading.models import Bot, BotOrder, StrategyConfig, TradingControl
from trading.tests.test_zone_strategy import PARAMS, T0, ZonePaperMatchesBacktestTests

TRADING = {"LIVE_TRADING": "false", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "WEBHOOK_URLS": [],
           "STALE_SERVICE_SECONDS": 180}


class FakeBook:
    def get_order_book(self, pair, limit=50, group="0"):
        return {"asks": [("1001", "0.5"), ("1002", "0.5"), ("1003", "5")],
                "bids": [("1000", "0.3"), ("999", "1")], "time": 0}


class FakeBroker:
    """Fills every order at the current test price, no fee."""

    def __init__(self):
        self.price = 100.0
        self.calls = []

    def market(self, pair, side, qty):
        self.calls.append((side, qty))
        return Fill(qty, self.price, 0.0, f"fake-{len(self.calls)}")


class BrokerTests(TestCase):
    def test_dry_run_walks_the_book(self):
        b = DryRunBroker(0.001, client=FakeBook())
        f = b.market("BTC_USDT", "buy", 1.2)
        self.assertAlmostEqual(f.avg_price, (1001 * 0.5 + 1002 * 0.5 + 1003 * 0.2) / 1.2)
        self.assertAlmostEqual(f.fee, 1.2 * f.avg_price * 0.001)
        s = b.market("BTC_USDT", "sell", 0.5)
        self.assertAlmostEqual(s.avg_price, (1000 * 0.3 + 999 * 0.2) / 0.5)
        deep = b.market("BTC_USDT", "sell", 5)  # deeper than the book: rest priced at the last level
        self.assertEqual(deep.qty, 5)

    def test_signature(self):
        body = '{"a": 1}'
        expected = hmac.new(b"sec", "\n".join(["POST", "/api/v4/spot/orders", "", hashlib.sha512(body.encode()).hexdigest(),
                                               "1700000000"]).encode(), hashlib.sha512).hexdigest()
        self.assertEqual(brokers.sign("sec", "post", "/api/v4/spot/orders", "", body, "1700000000"), expected)

    def test_gate_broker_market_order(self):
        class Resp:
            def __init__(self, data):
                self.ok, self.status_code, self._d, self.text = True, 200, data, ""

            def json(self):
                return self._d

        class Session:
            def __init__(self):
                self.sent = []

            def request(self, method, url, data=None, headers=None, timeout=None):
                self.sent.append((method, url, data, headers))
                return Resp({"id": "42", "status": "closed", "avg_deal_price": "1000", "filled_total": "100",
                             "fee": "0.2", "fee_currency": "USDT"})

        class Client:
            def pair_info(self, pair):
                return {"price_precision": 1, "amount_precision": 4}

            def get_ticker(self, pair):
                return {"last": "1000"}

        sess = Session()
        g = GateBroker("k", "s", session=sess, base_url="https://x/api/v4", client=Client())
        f = g.market("BTC_USDT", "buy", 0.1)
        method, url, data, headers = sess.sent[0]
        self.assertEqual((method, url), ("POST", "https://x/api/v4/spot/orders"))
        self.assertEqual(json.loads(data)["amount"], "100.0")  # market buys are sized in USDT
        self.assertEqual(headers["KEY"], "k")
        self.assertEqual(len(headers["SIGN"]), 128)
        self.assertEqual((f.qty, f.avg_price, f.fee, f.exchange_id), (0.1, 1000.0, 0.2, "42"))
        g.market("BTC_USDT", "sell", 0.123456)
        self.assertEqual(json.loads(sess.sent[1][2])["amount"], "0.1234")  # rounded down to the pair's precision
        with self.assertRaises(OrderError):
            GateBroker("", "")

    @override_settings(TRADING=TRADING)
    def test_live_is_locked_by_default(self):
        ok, missing = brokers.live_allowed()
        self.assertFalse(ok)
        self.assertEqual(len(missing), 3)


@override_settings(TRADING=TRADING)
class BotFlowTests(TestCase):
    setUp = ZonePaperMatchesBacktestTests.setUp

    def start(self, **risk):
        self.cfg.risk = risk
        self.cfg.save()
        bot = bots.start_bot(self.cfg)
        Bot.objects.filter(pk=bot.pk).update(started_at=T0 + timedelta(days=4))
        return bot

    def run_bot(self, bot, until_days=9, step_minutes=60):
        broker = FakeBroker()
        now = T0 + timedelta(days=4)
        while now < T0 + timedelta(days=until_days):
            now += timedelta(minutes=step_minutes)
            # the decision price is the close of the last fully closed 1m candle
            last = Candle.objects.filter(coin=self.coin, interval="1m", open_time__lt=now - timedelta(minutes=1)).last()
            broker.price = float(last.close)
            with mock.patch("trading.bots._last_price", return_value=broker.price):
                bots.step_bot(bot.pk, now=now, broker=broker)
        return broker

    def test_bot_mirrors_the_engine(self):
        bot = self.start()
        self.run_bot(bot)
        bot.refresh_from_db()
        buys = bot.orders.filter(side="buy", status="filled").count()
        sells = bot.orders.filter(side="sell", status="filled").count()
        model = bot.model_trades.count()
        self.assertGreater(buys, 0)
        self.assertEqual(sells, model)  # every engine exit was executed
        self.assertIn(buys - sells, (0, 1))  # possibly one still open
        self.assertEqual(bot.pos is not None, buys - sells == 1)
        self.assertFalse(bot.state["busy"])

    def test_late_entries_are_not_chased(self):
        """If the engine's simulated entry already happened inside a batch (e.g. after downtime), the bot
        stays flat instead of buying late at a different price; it re-joins on the next fresh signal."""
        bot = self.start()
        self.run_bot(bot, step_minutes=600)
        bot.refresh_from_db()
        self.assertLessEqual(bot.orders.filter(side="buy").count(), bot.model_trades.count() + 1)
        self.assertFalse(bot.state["busy"])

    def test_live_price_stop_and_target(self):
        bot = self.start()
        Bot.objects.filter(pk=bot.pk).update(state={**bot.state, "pos": {"qty": 1.0, "avg": 100.0, "stop": 95.0,
                                                                          "target": 110.0, "fee": 0.0}})
        fb = FakeBroker()
        fb.price = 96
        self.assertEqual(bots.on_price(self.coin, 96.0, broker=fb), 0)
        fb.price = 94.5
        self.assertEqual(bots.on_price(self.coin, 94.5, broker=fb), 1)
        o = BotOrder.objects.get(bot=bot)
        self.assertEqual((o.side, o.reason, o.pnl), ("sell", "stop (live price)", -5.5))
        self.assertIsNone(Bot.objects.get(pk=bot.pk).pos)
        self.assertEqual(bots.on_price(self.coin, 50.0, broker=fb), 0)  # flat: nothing more to do

    def test_order_sent_once_when_busy(self):
        bot = self.start()
        Bot.objects.filter(pk=bot.pk).update(state={**bot.state, "busy": True, "pos": {"qty": 1, "avg": 1, "stop": 0.5,
                                                                                        "target": None, "fee": 0}})
        self.assertIsNone(bots.execute(bot.pk, "sell", "x", ref_price=1.0, broker=FakeBroker()))

    def test_limits_block_entries_never_exits(self):
        bot = self.start()
        bots.halt("test")
        o = bots.execute(bot.pk, "buy", "signal", ref_price=100, stop=95, broker=FakeBroker())
        self.assertEqual((o.status, "kill switch" in o.message), ("blocked", True))
        Bot.objects.filter(pk=bot.pk).update(state={**Bot.objects.get(pk=bot.pk).state,
                                                    "pos": {"qty": 1, "avg": 100, "stop": 95, "target": None, "fee": 0}})
        self.assertEqual(bots.execute(bot.pk, "sell", "exit", ref_price=99, broker=FakeBroker()).status, "filled")
        bots.resume()
        TradingControl.objects.filter(pk=1).update(max_exposure=50)
        o = bots.execute(bot.pk, "buy", "signal", ref_price=100, stop=95, broker=FakeBroker())
        self.assertIn("exposure", o.message)

    def test_daily_loss_halts(self):
        bot = self.start(starting_cash=1000)
        TradingControl.get()
        TradingControl.objects.filter(pk=1).update(max_daily_loss_pct=2)
        BotOrder.objects.create(bot=bot, side="sell", qty=1, ref_price=1, status="filled", pnl=-25)
        o = bots.execute(bot.pk, "buy", "signal", ref_price=100, stop=95, broker=FakeBroker())
        self.assertEqual(o.status, "blocked")
        self.assertTrue(TradingControl.get().halted)

    def test_kill_switch(self):
        bot = self.start()
        Bot.objects.filter(pk=bot.pk).update(state={**bot.state, "pos": {"qty": 2, "avg": 100, "stop": 90,
                                                                          "target": None, "fee": 0}})
        with mock.patch("trading.bots._last_price", return_value=101.0):
            self.assertEqual(bots.kill(by="test", broker=FakeBroker()), 1)
        bot.refresh_from_db()
        self.assertEqual((bot.status, bot.pos), ("stopped", None))
        self.assertTrue(TradingControl.get().halted)

    def test_live_start_refused(self):
        with self.assertRaises(ValueError):
            bots.start_bot(self.cfg, Bot.Mode.LIVE)
        p = bots.promotion_status(self.cfg)
        self.assertFalse(p["ok"])


@override_settings(TRADING={**TRADING, "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "123",
                            "WEBHOOK_URLS": ["https://hook.test/a"]})
class AlertAndTelegramTests(TestCase):
    def test_notify_sends_both(self):
        sent = []

        class Http:
            RequestException = Exception

            def post(self, url, json=None, timeout=None):
                sent.append((url, json))
                return type("R", (), {"ok": True, "status_code": 200})()

        with mock.patch("trading.notify.requests", Http()):
            notify.notify("test", "hello", {"x": 1}, background=False)
        self.assertTrue(any("api.telegram.org/bott/sendMessage" in u for u, _ in sent))
        self.assertTrue(any(u == "https://hook.test/a" and j["event"] == "test" for u, j in sent))

    def test_commands(self):
        with mock.patch("trading.notify.notify"):
            self.assertIsNone(telegram.handle("/status", "999"))  # strangers are ignored
            self.assertIn("No bots running", telegram.handle("/status", "123"))
            self.assertIn("halted", telegram.handle("/stop", "123"))
            self.assertTrue(TradingControl.get().halted)
            self.assertIn("allowed", telegram.handle("/start", "123"))
            self.assertIn("/kill confirm", telegram.handle("/kill", "123"))
            self.assertIn("Usage", telegram.handle("/forceexit", "123"))
            self.assertIn("/help", telegram.handle("/nope", "123"))


@override_settings(TRADING=TRADING)
class ControlViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("me", password="not-a-real-pass-123")
        self.client.force_login(self.user)
        coin = Coin.objects.create(symbol="BTC", chain="BTC", gate_currency="BTC", verified_at=dj_tz.now())
        self.cfg = StrategyConfig.objects.create(owner=self.user, name="z", strategy_key="zone_hotspot", coin=coin,
                                                 interval="1h", params=PARAMS, risk={})

    def test_pages_and_actions(self):
        self.assertContains(self.client.get(reverse("trading:control")), "Kill switch")
        self.client.post(reverse("trading:control"), {"action": "halt", "reason": "lunch"})
        self.assertTrue(TradingControl.for_user(self.user).halted)
        self.client.post(reverse("trading:control"), {"action": "resume"})
        self.assertFalse(TradingControl.for_user(self.user).halted)
        r = self.client.post(reverse("trading:control"), {"action": "kill", "confirm": "nope"}, follow=True)
        self.assertContains(r, "Type KILL")
        self.client.post(reverse("trading:control"), {"action": "live", "enable": "1", "confirm": "LIVE"})
        self.assertTrue(TradingControl.for_user(self.user).live_enabled)
        self.client.post(reverse("trading:control"), {"action": "limits", "max_daily_loss_pct": "1.5",
                                                      "max_exposure": "500", "max_orders_per_hour": "5",
                                                      "min_dry_run_days": "3"})
        c = TradingControl.for_user(self.user)
        self.assertEqual((c.max_daily_loss_pct, c.max_exposure, c.max_orders_per_hour, c.min_dry_run_days),
                         (1.5, 500, 5, 3))

        self.assertContains(self.client.get(reverse("trading:strategy_detail", args=[self.cfg.pk])), "Start dry-run bot")
        r = self.client.post(reverse("trading:bot_start", args=[self.cfg.pk]), {"mode": "dry_run"})
        bot = Bot.objects.get()
        self.assertRedirects(r, reverse("trading:bot_detail", args=[bot.pk]), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("trading:bot_detail", args=[bot.pk])), "DRY-RUN")
        r = self.client.post(reverse("trading:bot_start", args=[self.cfg.pk]), {"mode": "live"}, follow=True)
        self.assertContains(r, "Not ready for live")
        self.client.post(reverse("trading:bot_stop", args=[bot.pk]))
        self.assertEqual(Bot.objects.get(pk=bot.pk).status, "stopped")
