"""Portfolio ledger: exact storage, average-cost and FIFO P&L, recording QF-gate fills, the Gate.io sync, pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from ledger import gate_sync, pnl, recorder, reports
from ledger.models import Account, SyncState, Transaction
from market.models import Coin

D = Decimal
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def tx(i, side, qty, price, *, fee="0", fc="USDT", fee_quote=None, kind="trade", market="spot", base="ETH",
       quote="USDT"):
    fee = D(fee)
    return SimpleNamespace(id=i, executed_at=T0 + timedelta(hours=i), kind=kind, market=market, base=base, quote=quote,
                           side=side, qty=D(qty), price=None if price is None else D(price), fee=fee, fee_currency=fc,
                           fee_quote=D(fee_quote) if fee_quote is not None else (fee if fc == quote else None))


class ExactStorageTests(TestCase):
    def test_decimals_round_trip_exactly(self):
        acc = Account.objects.create(name="X")
        v = D("0.123456789012345678901234567")
        t = Transaction.objects.create(account=acc, base="ETH", side="buy", qty=v, price=D("0.1"),
                                       executed_at=T0, source_key="k")
        t.refresh_from_db()
        self.assertEqual(t.qty, v)
        self.assertEqual(t.price * 3, D("0.3"))  # not 0.30000000000000004
        self.assertEqual(t.fee, D(0))


class PnlTests(TestCase):
    rows = [tx(1, "buy", "1", "100"), tx(2, "buy", "1", "200"), tx(3, "sell", "1", "300")]

    def test_average_cost(self):
        b = pnl.compute(self.rows, "avg")[("spot", "ETH", "USDT")]
        self.assertEqual(b.realised, D(150))
        self.assertEqual(b.qty, D(1))
        self.assertEqual(b.cost, D(150))
        self.assertEqual(b.avg_cost, D(150))

    def test_fifo(self):
        b = pnl.compute(self.rows, "fifo")[("spot", "ETH", "USDT")]
        self.assertEqual(b.realised, D(200))
        self.assertEqual(b.cost, D(200))

    def test_methods_agree_once_fully_sold(self):
        rows = self.rows + [tx(4, "sell", "1", "250")]
        a, f = (pnl.compute(rows, m)[("spot", "ETH", "USDT")] for m in ("avg", "fifo"))
        self.assertEqual(a.realised, f.realised)
        self.assertEqual(a.realised, D(250))
        self.assertEqual(a.qty, D(0))

    def test_default_is_average_cost(self):
        self.assertEqual(pnl.DEFAULT_METHOD, "avg")
        self.assertEqual(pnl.compute(self.rows)[("spot", "ETH", "USDT")].realised, D(150))

    def test_fees_in_quote_and_in_coin(self):
        rows = [tx(1, "buy", "1", "100", fee="0.001", fc="ETH"),  # receive 0.999 ETH for 100 USDT
                tx(2, "sell", "0.999", "110", fee="0.2", fc="USDT")]
        b = pnl.compute(rows)[("spot", "ETH", "USDT")]
        self.assertEqual(b.qty, D(0))
        self.assertEqual(b.realised, D("0.999") * 110 - D("0.2") - 100)
        self.assertEqual(b.fees, D("0.1") + D("0.2"))

    def test_fee_paid_in_gt_spends_gt(self):
        rows = [tx(1, "buy", "10", "20", base="GT"),
                tx(2, "buy", "1", "100", fee="0.01", fc="GT", fee_quote="0.25")]
        books = pnl.compute(rows)
        eth, gt = books[("spot", "ETH", "USDT")], books[("spot", "GT", "USDT")]
        self.assertEqual(eth.cost, D("100.25"))
        self.assertEqual(gt.qty, D("9.99"))
        self.assertEqual(gt.realised, D("0.25") - D("0.2"))  # GT spent at 25 that cost 20

    def test_spot_oversell_is_flagged_not_shorted(self):
        b = pnl.compute([tx(1, "buy", "1", "100"), tx(2, "sell", "3", "120")])[("spot", "ETH", "USDT")]
        self.assertEqual(b.qty, D(0))
        self.assertEqual(b.realised, D(20))
        self.assertEqual(b.unmatched_qty, D(2))
        self.assertTrue(any("history missing" in f for f in b.flags))

    def test_margin_short(self):
        b = pnl.compute([tx(1, "sell", "2", "100", market="margin"), tx(2, "buy", "2", "80", market="margin")])
        self.assertEqual(b[("margin", "ETH", "USDT")].realised, D(40))

    def test_deposit_and_withdrawal(self):
        rows = [tx(1, "in", "2", None, kind="deposit"), tx(2, "out", "1", None, kind="withdrawal"),
                tx(3, "in", "1", "50", kind="deposit")]
        b = pnl.compute(rows, "fifo")[("spot", "ETH", "USDT")]
        self.assertEqual(b.qty, D(2))
        self.assertEqual(b.unknown_cost_qty, D(2))
        self.assertEqual(b.realised, D(0))
        self.assertTrue(b.flags)

    def test_balances(self):
        rows = [tx(1, "in", "1000", None, kind="deposit", base="USDT"),
                tx(2, "buy", "1", "100", fee="0.001", fc="ETH"), tx(3, "sell", "0.5", "120", fee="0.12")]
        bal = pnl.balances(rows)
        self.assertEqual(bal["ETH"], D("0.499"))
        self.assertEqual(bal["USDT"], D(1000) - 100 + 60 - D("0.12"))

    def test_period_counts_only_sales_inside(self):
        rows = self.rows + [tx(10, "sell", "1", "400")]
        rep = reports.period(rows, "avg", T0 + timedelta(hours=5), None)
        self.assertEqual(len(rep["rows"]), 1)
        r = rep["rows"][0]
        self.assertEqual(r["sold_qty"], D(1))
        self.assertEqual(r["realised"], D(250))  # 400 - average cost 150
        self.assertEqual(r.get("bought_qty", D(0)), D(0))


class RecorderTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(symbol="ETH", chain="ETH", gate_currency="ETH", verified_at=dj_tz.now())

    def test_cascade_fills_once(self):
        from cascades.models import Cascade, CascadeLevel

        c = Cascade.objects.create(coin=self.coin, name="t")
        CascadeLevel.objects.create(cascade=c, index=1, target_price=100, amount_usdt=10, take_profit_pct=5,
                                    status="filled", fill_price=100, filled_at=T0, qty=0.0998, buy_fee=0.02,
                                    sell_status="filled", sell_target=105, sell_filled_at=T0 + timedelta(hours=1),
                                    sell_fee=0.020958, pnl=0.458042)
        self.assertEqual(recorder.record_internal(), 2)
        self.assertEqual(recorder.record_internal(), 0)
        rows = list(Transaction.objects.all())
        self.assertEqual({r.account.name for r in rows}, {"Dry-run"})
        b = pnl.compute(rows)[("spot", "ETH", "USDT")]
        self.assertEqual(b.qty, D(0))
        self.assertAlmostEqual(float(b.realised), 0.458042, places=9)  # = the level's own pnl

    def test_paper_trade_matches_its_pnl(self):
        from trading.models import PaperSession, StrategyConfig, Trade

        cfg = StrategyConfig.objects.create(name="p", strategy_key="pdh_breakout", coin=self.coin, interval="1h")
        s = PaperSession.objects.create(config=cfg, strategy_key="pdh_breakout", interval="1h", status="stopped")
        Trade.objects.create(paper=s, side="long", entry_time=T0, entry_price=100, exit_time=T0 + timedelta(hours=2),
                             exit_price=110, qty=2, fees=0.42, pnl=19.58, r_multiple=1)
        Trade.objects.create(paper=s, side="short", entry_time=T0 + timedelta(hours=3), entry_price=110,
                             exit_time=T0 + timedelta(hours=4), exit_price=100, qty=1, fees=0.21, pnl=9.79, r_multiple=1)
        self.assertEqual(recorder.record_internal(), 4)
        books = pnl.compute(Transaction.objects.all())
        self.assertAlmostEqual(float(books[("spot", "ETH", "USDT")].realised), 19.58, places=9)
        self.assertAlmostEqual(float(books[("margin", "ETH", "USDT")].realised), 9.79, places=9)
        self.assertEqual(Transaction.objects.first().account.name, "Paper")

    def test_bot_orders_dry_run_and_live(self):
        from trading.models import Bot, BotOrder, StrategyConfig

        cfg = StrategyConfig.objects.create(name="b", strategy_key="pdh_breakout", coin=self.coin, interval="1h")
        dry = Bot.objects.create(config=cfg, mode="dry_run", strategy_key="pdh_breakout", interval="1h", status="stopped")
        live = Bot.objects.create(config=cfg, mode="live", strategy_key="pdh_breakout", interval="1h", status="stopped")
        BotOrder.objects.create(bot=dry, side="buy", qty=1, ref_price=100, status="filled", filled_qty=1, avg_price=100, fee=0.2)
        BotOrder.objects.create(bot=live, side="buy", qty=1, ref_price=100, status="filled", filled_qty=1, avg_price=100,
                                fee=0.2, exchange_id="555")
        BotOrder.objects.create(bot=dry, side="buy", qty=1, ref_price=100, status="blocked")
        self.assertEqual(recorder.record_internal(), 2)
        live_row = Transaction.objects.get(account__name="Gate.io")
        self.assertTrue(live_row.provisional)
        self.assertEqual(live_row.order_id, "555")


class FakeClient:
    """Answers signed GETs from fixed data and records every request."""

    def __init__(self, trades=(), deposits=(), withdrawals=(), accounts=()):
        self.data = {"/spot/my_trades": list(trades), "/wallet/deposits": list(deposits),
                     "/wallet/withdrawals": list(withdrawals), "/spot/accounts": list(accounts)}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/spot/accounts":
            return self.data[path]
        rows = [r for r in self.data[path]
                if params["from"] <= int(float(r.get("create_time") or r.get("timestamp"))) <= params["to"]]
        if "page" in params:
            lim = params["limit"]
            return rows[(params["page"] - 1) * lim: params["page"] * lim]
        return rows[params["offset"]: params["offset"] + params["limit"]]


def gate_trade(i, at, side="buy", amount="0.5", price="2000", fee="0.0005", fc="ETH", order="9"):
    ts = int(at.timestamp())
    return {"id": str(i), "create_time": str(ts), "create_time_ms": f"{ts * 1000}.000", "currency_pair": "ETH_USDT",
            "side": side, "role": "taker", "amount": amount, "price": price, "order_id": order, "fee": fee,
            "fee_currency": fc, "point_fee": "0", "gt_fee": "0", "text": "web"}


@override_settings(LEDGER={"GATE_KEY": "k", "GATE_SECRET": "s", "HISTORY_START": "2026-01-01"})
class GateSyncTests(TestCase):
    def setUp(self):
        self.acc = recorder.account(recorder.GATE)
        self.now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        self.no_price = lambda pair, at: D("2000")

    def test_imports_trades_once_and_walks_back(self):
        old = gate_trade(1, datetime(2026, 3, 5, tzinfo=timezone.utc))
        new = gate_trade(2, datetime(2026, 11, 10, tzinfo=timezone.utc), side="sell", fee="1", fc="USDT")
        client = FakeClient(trades=[old, new])
        for _ in range(3):
            gate_sync.sync_stream(self.acc, "spot_trades", client, self.now, max_windows=6, price_lookup=self.no_price)
        st = SyncState.objects.get(account=self.acc, stream="spot_trades")
        self.assertTrue(st.backfill_complete)
        self.assertEqual(Transaction.objects.filter(account=self.acc).count(), 2)
        for path, p in client.calls:
            self.assertLessEqual(p["to"] - p["from"], 30 * 86400)  # Gate's 30-day limit
        sell = Transaction.objects.get(source_key="gate:spot:trade:2")
        self.assertEqual((sell.side, sell.qty, sell.price, sell.fee_quote), ("sell", D("0.5"), D("2000"), D("1")))
        buy = Transaction.objects.get(source_key="gate:spot:trade:1")
        self.assertEqual(buy.fee_quote, D("0.0005") * 2000)
        # a second full sync adds nothing
        st.newest_done = None
        st.save()
        self.assertEqual(gate_sync.sync_stream(self.acc, "spot_trades", client, self.now, price_lookup=self.no_price), 0)

    def test_exact_trades_replace_a_provisional_live_order(self):
        Transaction.objects.create(account=self.acc, base="ETH", side="buy", qty=D("0.5"), price=D("2001"),
                                   executed_at=self.now, source_key="botorder:1", order_id="9", provisional=True, route="bot")
        n = gate_sync.store(self.acc, "spot_trades", [gate_trade(5, self.now)], self.no_price)
        self.assertEqual(n, 1)
        self.assertFalse(Transaction.objects.filter(provisional=True).exists())
        self.assertEqual(Transaction.objects.get().source_key, "gate:spot:trade:5")

    def test_transfers_and_balance_check(self):
        dep = {"id": "d1", "timestamp": str(int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())),
               "currency": "USDT", "amount": "1000", "status": "DONE", "chain": "TRX"}
        pending = {**dep, "id": "d2", "status": "PEND"}
        gate_sync.store(self.acc, "deposits", [dep, pending], self.no_price)
        gate_sync.store(self.acc, "spot_trades", [gate_trade(1, datetime(2026, 11, 2, tzinfo=timezone.utc))], self.no_price)
        client = FakeClient(accounts=[{"currency": "USDT", "available": "0", "locked": "0"},
                                      {"currency": "ETH", "available": "0.4995", "locked": "0"}])
        chk = gate_sync.check_balances(self.acc, client)
        rows = {r["currency"]: r for r in chk.rows}
        self.assertTrue(rows["ETH"]["ok"])  # 0.5 bought, 0.0005 fee taken in ETH
        self.assertNotIn("USDT", rows)  # 1000 deposited, 1000 spent: zero on both sides (the pending deposit is not counted)
        self.assertEqual(pnl.balances(self.acc.transactions.all())["USDT"], D(0))
        self.assertTrue(chk.ok)
        client.data["/spot/accounts"][1]["available"] = "0.5"
        bad = gate_sync.check_balances(self.acc, client)
        self.assertFalse(bad.ok)
        self.assertIn("not finished", bad.message)


class PageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("u", "u@example.com", "pw")
        self.client.force_login(self.user)
        self.acc = recorder.account(recorder.GATE, self.user)
        for i, (side, q, p) in enumerate([("buy", "1", "100"), ("buy", "1", "200"), ("sell", "1", "300")]):
            Transaction.objects.create(account=self.acc, base="ETH", side=side, qty=D(q), price=D(p), fee=D(0),
                                       executed_at=T0 + timedelta(days=i), source_key=f"t{i}", route="gate")
        patcher = mock.patch("ledger.views.live_prices", return_value={"ETH_USDT": D("250")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_holdings_average_by_default_and_fifo(self):
        r = self.client.get(reverse("ledger:index"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Average cost")
        self.assertContains(r, "150.00")  # realised and cost by average
        r = self.client.get(reverse("ledger:index"), {"method": "fifo"})
        self.assertContains(r, "FIFO")
        self.assertContains(r, "200.00")

    def test_report_and_csv(self):
        r = self.client.get(reverse("ledger:report"), {"asset": "eth", "start": "2026-11-03", "end": "2026-11-03"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Every transaction in the period (1)")
        csv = self.client.get(reverse("ledger:report"), {"format": "csv"}).content.decode("utf-8-sig")
        self.assertIn("realised_pnl", csv)
        self.assertIn("ETH,USDT", csv)
        tcsv = self.client.get(reverse("ledger:transactions"), {"format": "csv"}).content.decode("utf-8-sig")
        self.assertEqual(len(tcsv.strip().splitlines()), 4)

    def test_manual_entry_and_pages(self):
        r = self.client.post(reverse("ledger:tx_new"), {
            "account": self.acc.pk, "kind": "trade", "market": "spot", "side": "buy", "base": "btc", "quote": "usdt",
            "qty": "0.01", "price": "60000", "fee": "0.6", "fee_currency": "usdt", "executed_at": "2026-11-05T10:00",
            "note": "bought on the app"})
        self.assertEqual(r.status_code, 302)
        t = Transaction.objects.get(base="BTC")
        self.assertEqual((t.route, t.fee_quote, t.qty), ("manual", D("0.6"), D("0.01")))
        for name in ("ledger:transactions", "ledger:accounts"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("ledger:tx_edit", args=[t.pk])).status_code, 200)
        self.client.post(reverse("ledger:tx_delete", args=[t.pk]))
        self.assertFalse(Transaction.objects.filter(base="BTC").exists())
        # recorded fills can't be deleted
        self.client.post(reverse("ledger:tx_delete", args=[Transaction.objects.first().pk]))
        self.assertEqual(Transaction.objects.count(), 3)

    def test_sync_button_without_key(self):
        r = self.client.post(reverse("ledger:account_action", args=[self.acc.pk, "sync"]), follow=True)
        self.assertContains(r, "read-only key")
