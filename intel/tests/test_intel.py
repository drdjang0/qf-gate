"""Phase 7: news (7.2), LLM layer (7.3), briefs (7.4), macro (7.5), scorecard + memory (7.6), AI filter (7.7)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from intel import briefs, gate, llm, macro, news, scorecard
from intel.models import Brief, LLMCall, NewsItem
from market.models import Coin
from trading.engine import Bar, Engine
from trading.strategies.base import Signal

INTEL = {
    "LLM_PROVIDER": "anthropic", "LLM_MODEL": "claude-opus-5-5", "LLM_QUICK_MODEL": "", "LLM_BASE_URL": "", "LLM_API_KEY": "k",
    "LLM_DAILY_BUDGET_USD": 1.0, "LLM_MAX_CALLS_PER_DAY": 50, "LLM_PRICE_IN": "", "LLM_PRICE_OUT": "", "LLM_CACHE_HOURS": 6,
    "NEWS_EVERY_MINUTES": 0, "NEWS_FEEDS": ["https://feed.example/rss"], "NEWS_REDDIT": True, "NEWS_KEEP_DAYS": 30,
    "BRIEF_HORIZON_HOURS": 24, "DEBATE_ROUNDS": 1, "FILTER_MIN_BRIEFS": 3,
}
OFF = {**INTEL, "LLM_PROVIDER": ""}

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Solana price surges to record high</title><link>https://n.example/1</link><guid>g1</guid>
  <description>&lt;p&gt;SOL rally continues&lt;/p&gt;</description><pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Exchange hack: hackers drain hot wallet</title><link>https://n.example/2</link><guid>g2</guid></item>
<item><title>Researchers solve a SOLVENT puzzle</title><link>https://n.example/3</link></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>BTC ETF inflow</title><link href="https://a.example/x"/>
<id>a1</id><published>2026-11-16T09:00:00Z</published><summary>Bitcoin gains</summary></entry></feed>"""


class Resp:
    def __init__(self, data=None, text="", code=200):
        self._d, self.text, self.status_code, self.ok = data, text, code, code == 200

    def json(self):
        return self._d


class NewsTests(TestCase):
    def test_parse_score_match(self):
        items = news.parse_rss(RSS, "feed")
        self.assertEqual([i["title"][:10] for i in items], ["Solana pri", "Exchange h", "Researcher"])
        self.assertEqual(items[0]["summary"], "SOL rally continues")
        self.assertEqual(items[0]["published_at"].year, 2026)
        atom = news.parse_rss(ATOM, "atom")
        self.assertEqual((atom[0]["title"], atom[0]["url"]), ("BTC ETF inflow", "https://a.example/x"))
        self.assertEqual(news.score_text(items[0]["title"])[0], "bullish")
        self.assertEqual(news.score_text("Exchange hack drains wallet")[0], "bearish")
        self.assertEqual(news.score_text("Weekly roundup")[0], "neutral")
        m = news.matcher("SOL")
        self.assertTrue(m("Buy $SOL now") and m("SOL/USDT") and m("solana is up"))
        self.assertFalse(m("Researchers solve a SOLVENT puzzle"))

    def test_store_dedupes_and_is_point_in_time(self):
        t0 = dj_tz.now() - timedelta(hours=10)
        items = news.parse_rss(RSS, "feed")
        self.assertEqual(news.store(items, now=t0), 3)
        self.assertEqual(news.store(items, now=t0 + timedelta(hours=5)), 0)  # already stored: first_seen kept
        self.assertEqual(NewsItem.objects.get(uid=items[0]["uid"]).first_seen, t0)
        late = {**items[0], "uid": "late", "title": "Solana later headline"}
        news.store([late], now=t0 + timedelta(hours=8))
        seen = news.items_for("SOL", as_of=t0 + timedelta(hours=1))
        self.assertEqual([i.title[:6] for i in seen], ["Solana"])  # the later one is not visible yet
        self.assertEqual(len(news.items_for("SOL", as_of=t0 + timedelta(hours=9))), 2)
        self.assertEqual(news.mood(seen)["bullish"], 1)

    @override_settings(INTEL=INTEL)
    def test_fetch_all_survives_a_dead_source(self):
        class S:
            def get(self, url, **kw):
                if "reddit" in url:
                    return Resp(code=429)
                return Resp(text=RSS)
        st = news.fetch_all(session=S())
        self.assertTrue(st["feed.example"]["ok"])
        self.assertEqual(st["feed.example"]["new"], 3)
        self.assertEqual((st["reddit r/CryptoCurrency"]["ok"], st["reddit r/CryptoCurrency"]["error"]), (False, "HTTP 429"))
        self.assertEqual(news.source_status(), st)
        reddit = {"data": {"children": [{"data": {"id": "a", "title": "SOL to the moon", "permalink": "/r/x/a", "created_utc": 1}}]}}
        self.assertEqual(news.parse_reddit(reddit)[0]["url"], "https://www.reddit.com/r/x/a")


class MacroTests(TestCase):
    def test_parsers_and_partial_failure(self):
        csv_text = "observation_date,DFF\n" + "\n".join(f"2026-10-{d:02d},4.{d:02d}" for d in range(1, 26)) + "\n2026-11-16,."
        v = macro.parse_fred_csv(csv_text)
        self.assertEqual((v["date"], v["value"]), ("2026-10-25", 4.25))
        self.assertAlmostEqual(v["change"], 4.25 - 4.04)
        pm = macro.parse_polymarket([
            {"question": "Will Bitcoin reach $150k in 2026?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.23", "0.77"]',
             "volume24hr": 500, "endDate": "2026-12-31T00:00:00Z", "slug": "btc-150k"},
            {"question": "Who wins the election?", "outcomes": '["A", "B"]', "outcomePrices": '["0.5", "0.5"]', "volume24hr": 9e9},
        ])
        self.assertEqual(len(pm), 1)
        self.assertAlmostEqual(pm[0]["yes_pct"], 23)
        g = macro.parse_global({"data": {"market_cap_percentage": {"btc": 58.1, "eth": 11.2}, "total_market_cap": {"usd": 3e12},
                                         "market_cap_change_percentage_24h_usd": -1.5, "updated_at": 1794816000}})
        self.assertEqual((g["btc_dominance"], g["mcap_change_24h_pct"]), (58.1, -1.5))

        class S:
            def get(self, url, **kw):
                if "fred" in url:
                    return Resp(text=csv_text)
                if "coingecko" in url:
                    return Resp(code=503)
                return Resp(data=[])
        cache.clear()
        p = macro.refresh(session=S())
        self.assertEqual(len(p["fred"]), 3)
        self.assertEqual(p["errors"], {"crypto": "CoinGecko HTTP 503"})


@override_settings(INTEL=INTEL)
class LLMTests(TestCase):
    @override_settings(INTEL=OFF)
    def test_off_without_provider(self):
        self.assertFalse(llm.enabled())
        with self.assertRaises(llm.NotConfigured):
            llm.complete("s", "u")

    def test_logs_cost_caches_and_caps(self):
        with mock.patch.object(llm, "_call_provider", return_value=llm.Reply("hello", 1000, 500)) as call:
            self.assertEqual(llm.complete("s", "u", deep=True, purpose="t"), "hello")
            self.assertEqual(llm.complete("s", "u", deep=True, purpose="t"), "hello")  # cached: no 2nd provider call
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0], "claude-opus-5-5")
        c = LLMCall.objects.filter(cached=False).get()
        self.assertAlmostEqual(c.cost_usd, 1000 / 1e6 * 4 + 500 / 1e6 * 20)
        self.assertEqual(LLMCall.objects.filter(cached=True).count(), 1)
        LLMCall.objects.create(provider="anthropic", model="m", cost_usd=5.0)  # over the $1 cap
        with mock.patch.object(llm, "_call_provider") as call, self.assertRaises(llm.BudgetExceeded):
            llm.complete("s", "different", purpose="t")
        call.assert_not_called()

    def test_errors_are_logged(self):
        with mock.patch.object(llm, "_call_provider", side_effect=llm.LLMError("boom")), self.assertRaises(llm.LLMError):
            llm.complete("s", "u")
        self.assertEqual(LLMCall.objects.get().error, "boom")


def fake_snapshot(symbol="SOL", last=100.0):
    hs = [{"lower": 95.0, "upper": 97.0, "days": 5, "held": 4, "broke": 1, "dist_pct": -4.0}]
    d = {"interval": "1h", "first": 90.0, "last": last, "change_pct": 11.1, "high": 110.0, "low": 85.0, "range_pct": 29.4,
         "volume": 1e6, "held_pct": 60.0, "pos_in_range_pct": 60.0, "zones": 10, "touches": 30, "hotspots": hs}
    return {"symbol": symbol, "quote": "USDT", "exchange": "gate", "as_of": "2026-11-16 10:00 UTC", "d7": d, "d30": d,
            "last_price": last, "where": [{"exchange": "Gate.io", "last": last, "change_24h_pct": 1.0, "volume_24h": 5e5,
                                           "volume_share_pct": 40.0, "vs_median_pct": 0.1}],
            "btc_price": 90000.0, "news": {"mood": {"total": 0}, "items": []}, "macro": {}, "memory": {"recent": [], "lessons": []}}


def fake_llm(final: str):
    def call(model_id, system, user, max_tokens, effort):
        if "portfolio manager" in user:
            return llm.Reply(final, 100, 50)
        return llm.Reply("analysis text", 100, 50)
    return call


@override_settings(INTEL=INTEL)
class BriefTests(TestCase):
    def setUp(self):
        cache.clear()

    def run_brief(self, final, snap=None):
        b = briefs.create("sol", "usdt", "gate")
        with mock.patch.object(briefs, "build_snapshot", return_value=snap or fake_snapshot()), \
             mock.patch.object(llm, "_call_provider", side_effect=fake_llm(final)):
            return briefs.run(b, parallel=False)

    def test_full_run_with_grounding(self):
        final = json.dumps({"rating": "buy", "summary": "Price at 100 sits above the 95 to 97 hotspot; a move to 123.45 is possible.",
                            "entry": 100, "stop": 101, "target": 110, "key_risks": ["hotspot breaks"], "confidence": "medium"})
        b = self.run_brief(final)
        self.assertEqual((b.status, b.rating, b.symbol, b.price, b.btc_price), ("done", "Buy", "SOL", 100.0, 90000.0))
        self.assertEqual(b.calls, 11)  # 4 analysts, bull, bear, manager, 3 risk views, portfolio manager
        self.assertGreater(b.cost_usd, 0)
        self.assertEqual((b.plan["entry"], b.plan["stop"], b.plan["target"]), (100.0, None, 110.0))  # stop above entry dropped
        self.assertIn("stop 101.0 is not below the entry 100.0", b.checks["dropped_levels"][0])
        self.assertEqual(b.checks["unverified_numbers"], [123.45])  # not in the fact sheet
        self.assertIn("FACT SHEET for SOL/USDT", b.inputs["fact_sheet"])
        self.assertEqual(len(b.reports["debate"]), 2)

    def test_unreadable_answer_is_review_not_hold(self):
        b = self.run_brief("I think it is fine, probably.")
        self.assertEqual((b.status, b.rating, b.plan["entry"]), ("done", "Review", None))
        self.assertEqual(briefs.parse_final('{"rating": "Strong buy"}')["rating"], "Review")

    def test_failures_are_recorded(self):
        b = briefs.create("SOL", "USDT", "gate")
        with mock.patch.object(briefs, "build_snapshot", return_value=fake_snapshot()), \
             mock.patch.object(llm, "_call_provider", side_effect=llm.LLMError("Anthropic API 401: bad key")):
            b = briefs.run(b, parallel=False)
        self.assertEqual((b.status, b.error), ("failed", "Anthropic API 401: bad key"))

    @override_settings(INTEL=OFF)
    def test_create_needs_a_provider(self):
        with self.assertRaises(llm.NotConfigured):
            briefs.create("SOL", "USDT", "gate")

    def test_queue_claims_once_and_fails_stale(self):
        b = briefs.create("SOL", "USDT", "gate")
        with mock.patch.object(briefs.threading, "Thread") as th:
            self.assertEqual(briefs.run_queued(), 1)
            self.assertEqual(briefs.run_queued(), 0)  # one is running
            self.assertFalse(briefs.start(b.pk))  # already claimed
        th.return_value.start.assert_called_once()
        Brief.objects.filter(pk=b.pk).update(started_at=dj_tz.now() - timedelta(minutes=30))
        briefs.run_queued()
        self.assertEqual(Brief.objects.get(pk=b.pk).status, "failed")


def done_brief(rating, alpha, symbol="SOL", hours_ago=48, settled=True):
    t = dj_tz.now() - timedelta(hours=hours_ago)
    return Brief.objects.create(symbol=symbol, quote="USDT", status="done", rating=rating, price=100, btc_price=50000,
                                created_at=t, finished_at=t + timedelta(minutes=3),
                                settled_at=(t + timedelta(hours=25)) if settled else None,
                                ret_pct=alpha + 1 if settled else None, btc_ret_pct=1 if settled else None,
                                alpha_pct=alpha if settled else None)


@override_settings(INTEL=INTEL)
class ScorecardTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_settle_due(self):
        b = done_brief("Buy", 0, settled=False, hours_ago=30)
        fresh = done_brief("Buy", 0, settled=False, hours_ago=2)
        prices = {"SOL": 110.0, "BTC": 51000.0}
        n = scorecard.settle_due(price_fn=lambda ex, base, quote, when: prices[base])
        self.assertEqual(n, 1)
        b.refresh_from_db()
        self.assertAlmostEqual(b.ret_pct, 10.0)
        self.assertAlmostEqual(b.btc_ret_pct, 2.0)
        self.assertAlmostEqual(b.alpha_pct, 8.0)
        fresh.refresh_from_db()
        self.assertIsNone(fresh.settled_at)  # horizon not over yet

    def test_scorecard_edge_memory_and_filter(self):
        done_brief("Buy", 3.0)
        done_brief("Buy", -1.0)
        done_brief("Sell", -2.0)
        done_brief("Hold", 5.0)
        s = scorecard.scorecard()
        self.assertEqual((s["settled"], s["directional"]), (4, 3))
        self.assertAlmostEqual(s["edge"], (3.0 - 1.0 + 2.0) / 3)
        self.assertAlmostEqual(s["hit_pct"], 200 / 3)
        buy = next(r for r in s["by_rating"] if r["rating"] == "Buy")
        self.assertEqual((buy["n"], buy["avg_alpha"], buy["hit_pct"]), (2, 1.0, 50.0))
        self.assertTrue(scorecard.filter_earned()["ok"])  # 3 directional (test minimum), edge > 0, hit rate > 50%
        mem = scorecard.memory_for("SOL", "USDT", before=dj_tz.now())
        self.assertEqual(len(mem["recent"]), 3)
        self.assertEqual({r["verdict"] for r in mem["recent"]}, {"right", "wrong", "not scored"})
        self.assertEqual(scorecard.memory_for("SOL", "USDT", before=dj_tz.now() - timedelta(days=5))["recent"], [])


class Strat:
    key = "stub"
    lookback = 2
    p = {}

    def __init__(self, sig):
        self.sig = sig

    def on_bar(self, ctx):
        return self.sig


@override_settings(INTEL=INTEL)
class AIFilterTests(TestCase):
    def setUp(self):
        cache.clear()
        self.coin = Coin.objects.create(symbol="SOL", chain="SOL", gate_currency="SOL")
        self.bar = Bar(time=int(dj_tz.now().timestamp()) // 3600 * 3600, open=1, high=1, low=1, close=1, step=3600)

    def test_only_paper_and_dry_run_when_switched_on(self):
        self.assertIsNone(gate.make_gate(self.coin, {"ai_min_rating": "off"}, "paper"))
        self.assertIsNone(gate.make_gate(self.coin, {"ai_min_rating": "buy"}, "live"))
        self.assertIsNotNone(gate.make_gate(self.coin, {"ai_min_rating": "buy"}, "dry_run"))
        self.assertIn("ignored", gate.status({"ai_min_rating": "buy"}, "live")["why"])

    def test_inactive_until_earned(self):
        g = gate.make_gate(self.coin, {"ai_min_rating": "buy"}, "paper")
        self.assertIsNone(g(self.bar, Signal("buy", stop=0.9)))  # scorecard empty: blocks nothing
        self.assertFalse(gate.status({"ai_min_rating": "buy"}, "paper")["active"])

    def test_point_in_time_rules_once_earned(self):
        g = gate.make_gate(self.coin, {"ai_min_rating": "overweight", "ai_max_age_hours": 6}, "paper")
        close = datetime.fromtimestamp(self.bar.time + 3600, tz=timezone.utc)
        with mock.patch.object(gate, "filter_earned", return_value={"ok": True}):
            self.assertEqual(g(self.bar, Signal("buy", stop=0.9)), "AI filter: no recent brief")
            Brief.objects.create(symbol="SOL", quote="USDT", status="done", rating="Buy",
                                 finished_at=close + timedelta(minutes=1))  # finished after the bar closed: invisible
            self.assertEqual(g(self.bar, Signal("buy", stop=0.9)), "AI filter: no recent brief")
            Brief.objects.create(symbol="SOL", quote="USDT", status="done", rating="Hold", finished_at=close - timedelta(hours=1))
            self.assertEqual(g(self.bar, Signal("buy", stop=0.9)), "AI filter: brief rated Hold")
            Brief.objects.create(symbol="SOL", quote="USDT", status="done", rating="Buy", finished_at=close - timedelta(minutes=30))
            self.assertIsNone(g(self.bar, Signal("buy", stop=0.9)))
            self.assertEqual(g(self.bar, Signal("short", stop=1.1)), "AI filter: brief rated Buy")  # shorts need bearish

    def test_engine_hook_blocks_and_counts(self):
        bars = [Bar(time=3600 * i, open=100, high=101, low=99, close=100, step=3600) for i in range(1, 4)]
        e = Engine(Strat(Signal("buy", stop=95)), {}, "UTC")
        e.entry_gate = lambda bar, sig: "AI filter: no recent brief"
        for b in bars:
            e.process(b)
        self.assertIsNone(e.pos)
        self.assertEqual(e.blocked["AI filter: no recent brief"], 3)
        self.assertNotIn("entry_gate", e.state())
        plain = Engine(Strat(Signal("buy", stop=95)), {}, "UTC")
        for b in bars:
            plain.process(b)
        self.assertIsNotNone(plain.pos)  # no gate: unchanged behaviour


@override_settings(INTEL=INTEL)
class PageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(User.objects.create_user("i", password="not-a-real-pass-123"))
        cache.set(macro.CACHE_KEY, {"fetched_at": "now", "errors": {}, "fred": [], "crypto": {}, "polymarket": []})

    def test_index_card_and_brief_pages(self):
        news.store(news.parse_rss(RSS, "feed"))
        page = self.client.get(reverse("intel:index"))
        self.assertContains(page, "Solana price surges")
        self.assertContains(page, "Run brief")
        card = self.client.get(reverse("intel:card") + "?symbol=SOL&quote=USDT")
        self.assertContains(card, "Solana price surges")
        self.assertNotContains(card, "Exchange hack")
        self.assertContains(card, "AI brief")
        with mock.patch.object(briefs, "start") as start:
            r = self.client.post(reverse("intel:brief_create"), {"symbol": "sol", "quote": "USDT", "exchange": "binance"})
        b = Brief.objects.get()
        self.assertRedirects(r, reverse("intel:brief_detail", args=[b.pk]))
        start.assert_called_once_with(b.pk)  # no worker running: started from the web server
        self.assertEqual((b.symbol, b.exchange), ("SOL", "binance"))
        self.assertContains(self.client.get(reverse("intel:brief_detail", args=[b.pk])), "Waiting to start")

    @override_settings(INTEL=OFF)
    def test_ai_hidden_without_provider(self):
        self.assertContains(self.client.get(reverse("intel:index")), "AI briefs are off")
        self.assertNotContains(self.client.get(reverse("intel:card") + "?symbol=SOL"), "AI brief</button>")
        r = self.client.post(reverse("intel:brief_create"), {"symbol": "SOL"})
        self.assertEqual(Brief.objects.count(), 0)
        self.assertRedirects(r, reverse("intel:index"), fetch_redirect_response=False)
