"""7.4 AI coin brief: a multi-agent debate over OUR data (idea from TradingAgents' analyst -> researcher ->
trader -> risk -> manager graph, re-implemented around QF-gate's own facts). Advisory only: a brief never
places an order and is never used in backtests or sweeps.

Flow (each step is one LLM call, logged and budget-capped by intel.llm):
1. A fact sheet is built from the app's own data: zones / hotspots / range stats (Scanner engine), where
   the coin trades on 7 exchanges, PDH / PDL and order flow (stored coins), funding, news seen so far,
   the macro panel and what earlier briefs for the coin got right or wrong (memory, 7.6).
2. Four analysts read it in parallel: technical, market structure, news / sentiment, macro.
3. A bull and a bear researcher debate (INTEL_DEBATE_ROUNDS), a research manager rules.
4. Three risk views (aggressive / neutral / conservative) challenge the plan.
5. A portfolio manager returns JSON: rating (Buy / Overweight / Hold / Underweight / Sell), summary,
   entry / stop / target, key risks.

Grounding: numbers come from the fact sheet, the model writes reasoning. After the run, levels that are
not on the right side of the price or far outside the observed range are dropped, and any price-like
number in the summary that is not in the fact sheet (or the checked levels) is listed as unverified.
Output that can't be read becomes "Review", never "Hold".
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from math import floor, log10

from django.conf import settings
from django.db import close_old_connections, connection
from django.db.models import Count, Sum
from django.utils import timezone

from market.exchanges import public
from market.exchanges.base import ExchangeError

from . import llm, macro, news, scorecard
from .models import Brief, LLMCall

log = logging.getLogger(__name__)
RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
STALE_MINUTES = 20


class BriefError(Exception):
    pass


# -- fact sheet ---------------------------------------------------------------------------------------


def _num(x, digits=6):
    """Round to `digits` significant digits (keeps the fact sheet short and the numbers checkable)."""
    if x is None:
        return None
    x = float(x)
    if x == 0:
        return 0.0
    return round(x, max(0, digits - 1 - int(floor(log10(abs(x))))))


def build_snapshot(symbol: str, quote: str, exchange: str, coin=None, now=None) -> dict:
    from market.services import scanner

    now = now or timezone.now()
    snap = {"symbol": symbol, "quote": quote, "exchange": exchange, "as_of": now.strftime("%Y-%m-%d %H:%M UTC")}
    for days in (7, 30):
        try:
            r = scanner.scan(exchange, symbol, quote, days, 1.0)
        except ExchangeError as exc:
            raise BriefError(f"{exchange} unreachable: {exc}") from exc
        if not r.get("stats"):
            raise BriefError(f"{public.EXCHANGES[exchange].name} has no {symbol}/{quote} candles")
        s = r["stats"]
        snap[f"d{days}"] = {"interval": r["interval"], **{k: _num(s[k]) for k in
                            ("first", "last", "change_pct", "high", "low", "range_pct", "volume", "held_pct", "pos_in_range_pct")},
                            "zones": s["zones"], "touches": s["touches"],
                            "hotspots": [{"lower": _num(h["lower"]), "upper": _num(h["upper"]), "days": h["touches"],
                                          "held": h["held"], "broke": h["broke"], "dist_pct": _num(h["dist"], 3)}
                                         for h in r["hotspots"][:6]]}
    snap["last_price"] = snap["d7"]["last"]
    snap["where"] = [{"exchange": w["name"], "last": _num(w.get("last")), "change_24h_pct": _num(w.get("change_pct"), 3),
                      "volume_24h": _num(w.get("quote_volume"), 4), "volume_share_pct": _num(w.get("volume_share_pct"), 3),
                      "vs_median_pct": _num(w.get("vs_median_pct"), 3)}
                     for w in scanner.where_it_trades(symbol, quote) if w.get("listed")]
    if coin is not None:
        snap["stored"] = _stored_facts(coin, now)
    if symbol == "BTC":
        snap["btc_price"] = snap["last_price"]
    else:
        try:
            btc = public.ticker("gate", "BTC", "USDT")
            snap["btc_price"] = float(btc["last"]) if btc else None
        except ExchangeError:
            snap["btc_price"] = None
    items = news.items_for(symbol, getattr(coin, "name", "") or "", as_of=now, hours=72, limit=15)
    snap["news"] = {"mood": news.mood(items), "items": [
        {"seen": it.first_seen.strftime("%Y-%m-%d %H:%M"), "source": it.source, "sentiment": it.sentiment,
         "title": it.title[:200]} for it in items]}
    m = macro.panel()
    snap["macro"] = {"fred": m.get("fred", []), "crypto": m.get("crypto", {}), "polymarket": m.get("polymarket", [])[:5]}
    snap["memory"] = scorecard.memory_for(symbol, quote, before=now)
    return snap


def _stored_facts(coin, now) -> dict:
    from market.models import Candle, DailyLevel, FundingRate

    days = list(DailyLevel.objects.filter(coin=coin, date__lte=now.date()).order_by("-date")[:8])
    out = {"daily": [{"date": str(d.date), "open": _num(d.open), "high": _num(d.high), "high_time": d.high_time.strftime("%H:%M"),
                      "low": _num(d.low), "low_time": d.low_time.strftime("%H:%M"), "close": _num(d.close),
                      "complete": d.is_complete} for d in days]}
    done = [d for d in days if d.is_complete and d.date < now.date()]
    if done:
        out["pdh"], out["pdl"] = _num(done[0].high), _num(done[0].low)
    agg = Candle.objects.filter(coin=coin, interval="1m", open_time__gte=now - timedelta(hours=24),
                                buy_volume__isnull=False).aggregate(b=Sum("buy_volume"), s=Sum("sell_volume"))
    if agg["b"] is not None:
        b, s = float(agg["b"]), float(agg["s"] or 0)
        out["delta_24h_pct"] = _num((b - s) / (b + s) * 100, 3) if b + s else 0.0
    fr = list(FundingRate.objects.filter(coin=coin).order_by("-time")[:3])
    if fr:
        out["funding_last_3"] = [{"time": f.time.strftime("%Y-%m-%d %H:%M"), "rate_pct": _num(f.rate * 100, 3)} for f in fr]
    return out


def facts_text(snap: dict) -> str:
    """The fact sheet every agent sees (deterministic text from the snapshot)."""
    L = [f"FACT SHEET for {snap['symbol']}/{snap['quote']} on {snap['exchange']}, as of {snap['as_of']}.",
         f"Last price: {snap['last_price']}."]
    for key, label in (("d7", "Last 7 days"), ("d30", "Last 30 days")):
        d = snap[key]
        L.append(f"{label} ({d['interval']} bars): change {d['change_pct']}%, high {d['high']}, low {d['low']}, "
                 f"range {d['range_pct']}%, price at {d['pos_in_range_pct']}% of the range, volume {d['volume']} {snap['quote']}, "
                 f"{d['zones']} zones, {d['touches']} zone touches, zones held {d['held_pct']}% of decided touches.")
        for h in d["hotspots"]:
            L.append(f"  hotspot {h['lower']} to {h['upper']}: revisited on {h['days']} bars, held {h['held']}, broke {h['broke']}, "
                     f"{h['dist_pct']}% from the price")
    if snap.get("where"):
        L.append("Where it trades (24h):")
        for w in snap["where"]:
            L.append(f"  {w['exchange']}: last {w['last']}, change {w['change_24h_pct']}%, volume {w['volume_24h']}, "
                     f"share {w['volume_share_pct']}%, vs median price {w['vs_median_pct']}%")
    st = snap.get("stored") or {}
    if "pdh" in st:
        L.append(f"Previous day high (PDH) {st['pdh']}, previous day low (PDL) {st['pdl']}.")
    for d in st.get("daily", [])[:5]:
        L.append(f"  day {d['date']}: open {d['open']}, high {d['high']} at {d['high_time']}, low {d['low']} at {d['low_time']}, "
                 f"close {d['close']}{'' if d['complete'] else ' (in progress)'}")
    if "delta_24h_pct" in st:
        L.append(f"Order flow last 24h: taker buy minus sell = {st['delta_24h_pct']}% of volume.")
    for f in st.get("funding_last_3", []):
        L.append(f"  perpetual funding {f['time']}: {f['rate_pct']}% per 8h")
    nw = snap.get("news") or {}
    mood = nw.get("mood") or {}
    L.append(f"News seen in the last 72h mentioning the coin: {mood.get('total', 0)} items "
             f"({mood.get('bullish', 0)} bullish, {mood.get('bearish', 0)} bearish, {mood.get('neutral', 0)} neutral by word count).")
    for it in nw.get("items", [])[:12]:
        L.append(f"  [{it['seen']}] ({it['source']}, {it['sentiment']}) {it['title']}")
    mc = snap.get("macro") or {}
    for f in mc.get("fred", []):
        L.append(f"Macro: {f['label']} = {f['value']} on {f['date']} (a month earlier {f['month_ago']}).")
    c = mc.get("crypto") or {}
    if c.get("btc_dominance") is not None:
        L.append(f"Crypto market: total cap change 24h {_num(c.get('mcap_change_24h_pct'), 3)}%, "
                 f"BTC dominance {_num(c.get('btc_dominance'), 3)}%, ETH dominance {_num(c.get('eth_dominance'), 3)}%.")
    for p in mc.get("polymarket", []):
        L.append(f"Prediction market: \"{p['question']}\" {p['outcome']} {_num(p['yes_pct'], 3)}% (ends {p['end']}).")
    mem = snap.get("memory") or {}
    if mem.get("recent"):
        L.append("Earlier briefs for this coin and what happened over their horizon:")
        for r in mem["recent"]:
            L.append(f"  {r['date']}: rated {r['rating']}, return {r['ret_pct']}%, vs BTC {r['alpha_pct']}%: {r['verdict']}")
    for line in mem.get("lessons", []):
        L.append(f"Track record: {line}.")
    if snap.get("btc_price"):
        L.append(f"BTC price now: {_num(snap['btc_price'])}.")
    return "\n".join(L)


# -- agents -------------------------------------------------------------------------------------------

SYSTEM = ("You are one member of a crypto research team inside a personal research app. You are given a FACT SHEET "
          "built from the app's own market data. Use only numbers that appear in the fact sheet; never invent prices, "
          "levels, statistics, dates or events. If something is not in the fact sheet, say it is unknown. This is "
          "research, not financial advice. Write plain English, at most about 200 words.")
ANALYSTS = {
    "technical": "You are the technical analyst. Read the price action: the 7-day and 30-day range, where the price sits "
                 "in it, the hotspot bands (support / resistance revisited on many bars), whether zones held or broke, "
                 "PDH / PDL and order flow if given. Say what the levels imply for the next 24 hours.",
    "structure": "You are the market-structure analyst. Read where the coin trades: which exchanges carry the volume, "
                 "whether prices agree across venues, liquidity, and funding if given. Say what this means for trading it.",
    "news": "You are the news and sentiment analyst. Read only the headlines in the fact sheet. Summarise what matters, "
            "note that the bullish / bearish labels are a crude word count, and say if there is too little news to judge.",
    "macro": "You are the macro analyst. Read the rates, dollar index, crypto market cap and dominance, and the "
             "prediction markets. Say whether the backdrop is a headwind or a tailwind, and how strongly.",
}
RISK_VIEWS = {
    "aggressive": "You are the aggressive risk analyst: argue for taking the opportunity, but name the levels that would prove you wrong.",
    "neutral": "You are the neutral risk analyst: weigh both sides and say what a balanced position would look like.",
    "conservative": "You are the conservative risk analyst: argue for protecting capital, and name what could go badly wrong.",
}
FINAL = ("You are the portfolio manager and make the final call for the next {h} hours. Reply with ONLY a JSON object, no "
         "other text, with these keys: \"rating\" (exactly one of Buy, Overweight, Hold, Underweight, Sell; commit to a side "
         "when the case is clear, Hold only when it truly is balanced), \"summary\" (3-5 sentences of reasoning), \"entry\", "
         "\"stop\", \"target\" (prices from the fact sheet levels, or null), \"key_risks\" (a list of short strings), "
         "\"confidence\" (low, medium or high). The app trades spot only: Sell / Underweight mean avoid or reduce, not short.")


def _ask(b_pk: int, role: str, system: str, user: str, deep: bool) -> str:
    try:
        return llm.complete(system, user, deep=deep, purpose=f"brief#{b_pk} {role}")
    finally:
        if threading.current_thread() is not threading.main_thread():
            connection.close()


def parse_final(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = str(data.get("rating") or "").strip().capitalize()
    risks = data.get("key_risks")
    return {"rating": raw if raw in RATINGS else "Review",
            "summary": str(data.get("summary") or "").strip() or (text or "").strip()[:2000],
            "entry": data.get("entry"), "stop": data.get("stop"), "target": data.get("target"),
            "key_risks": [str(x) for x in risks][:8] if isinstance(risks, list) else [],
            "confidence": str(data.get("confidence") or "").lower()}


_NUM = re.compile(r"(?<![\w.])\$?(\d[\d,]*(?:\.\d+)?)(?!\s*(?:%|[Rx]\b|h\b|hours?\b|days?\b|bars?\b|d\b))")


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text or ""):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def ground(final: dict, snap: dict, facts: str) -> tuple[dict, dict]:
    """Check the levels and the numbers in the summary against the fact sheet. Returns (plan, checks)."""
    last = snap["last_price"]
    lo, hi = snap["d30"]["low"], snap["d30"]["high"]
    plan, dropped = {}, []
    for k in ("entry", "stop", "target"):
        v = final.get(k)
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            v = None
        if v is not None and not (lo * 0.5 <= v <= hi * 1.5):
            dropped.append(f"{k} {v} is far outside the 30-day range")
            v = None
        plan[k] = v
    e = plan["entry"] or last
    if final["rating"] in ("Buy", "Overweight"):
        if plan["stop"] is not None and plan["stop"] >= e:
            dropped.append(f"stop {plan['stop']} is not below the entry {e}")
            plan["stop"] = None
        if plan["target"] is not None and plan["target"] <= e:
            dropped.append(f"target {plan['target']} is not above the entry {e}")
            plan["target"] = None
    elif final["rating"] in ("Hold", "Review"):
        if any(v is not None for v in plan.values()):
            dropped.append("levels ignored for a Hold / Review rating")
        plan = {"entry": None, "stop": None, "target": None}
    plan["key_risks"], plan["confidence"] = final.get("key_risks", []), final.get("confidence", "")
    known = _numbers(facts) + [v for v in (plan["entry"], plan["stop"], plan["target"]) if v]
    unverified = []
    for x in _numbers(final.get("summary", "")):
        if not (last * 0.2 <= x <= last * 5):
            continue  # not price-like for this coin
        if not any(abs(x - k) <= max(abs(k) * 0.005, 1e-12) for k in known):
            unverified.append(x)
    return plan, {"dropped_levels": dropped, "unverified_numbers": sorted(set(unverified))}


def run(b: Brief, parallel: bool = True) -> Brief:
    """Run a brief to the end (synchronously). Errors are stored on the brief, never raised."""
    def progress(text):
        b.progress = text
        b.save(update_fields=["progress"])

    def ask_all(jobs: dict) -> dict:
        if parallel:
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                futs = {k: ex.submit(_ask, b.pk, *args) for k, args in jobs.items()}
                return {k: f.result() for k, f in futs.items()}
        return {k: _ask(b.pk, *args) for k, args in jobs.items()}

    rounds = max(1, settings.INTEL["DEBATE_ROUNDS"])
    try:
        progress("Collecting data")
        snap = build_snapshot(b.symbol, b.quote, b.exchange, b.coin, now=b.created_at)
        facts = facts_text(snap)
        b.inputs, b.price, b.btc_price = {**snap, "fact_sheet": facts}, snap["last_price"], snap.get("btc_price")
        b.save(update_fields=["inputs", "price", "btc_price"])

        progress("Analysts reading the fact sheet")
        reports: dict = ask_all({k: (k, SYSTEM, f"{facts}\n\nYOUR ROLE: {v}", False) for k, v in ANALYSTS.items()})
        analyst_text = "\n\n".join(f"{k.upper()} ANALYST:\n{reports[k]}" for k in ANALYSTS)

        progress("Bull and bear debate")
        debate: list[str] = []
        for i in range(rounds):
            bull = _ask(b.pk, f"bull{i + 1}", SYSTEM, f"{facts}\n\n{analyst_text}\n\n" + "\n\n".join(debate) +
                        "\n\nYOUR ROLE: You are the bull researcher. Make the strongest honest case for buying, and answer "
                        "the bear's last points if there are any.", False)
            debate.append(f"BULL (round {i + 1}):\n{bull}")
            bear = _ask(b.pk, f"bear{i + 1}", SYSTEM, f"{facts}\n\n{analyst_text}\n\n" + "\n\n".join(debate) +
                        "\n\nYOUR ROLE: You are the bear researcher. Make the strongest honest case against buying, and "
                        "rebut the bull.", False)
            debate.append(f"BEAR (round {i + 1}):\n{bear}")
        reports["debate"] = debate
        debate_text = "\n\n".join(debate)

        progress("Research manager")
        reports["research_manager"] = _ask(b.pk, "research_manager", SYSTEM, f"{facts}\n\n{analyst_text}\n\n{debate_text}\n\n"
                                           "YOUR ROLE: You are the research manager. Decide which side argued better on the "
                                           f"facts, and propose a plan for the next {b.horizon_hours} hours with levels taken "
                                           "from the fact sheet.", True)
        plan_text = f"RESEARCH MANAGER PLAN:\n{reports['research_manager']}"

        progress("Risk review")
        reports["risk"] = ask_all({k: (f"risk_{k}", SYSTEM, f"{facts}\n\n{plan_text}\n\nYOUR ROLE: {v}", False)
                                   for k, v in RISK_VIEWS.items()})
        risk_text = "\n\n".join(f"{k.upper()} RISK VIEW:\n{v}" for k, v in reports["risk"].items())

        progress("Portfolio manager")
        final_raw = _ask(b.pk, "portfolio_manager", SYSTEM,
                         f"{facts}\n\n{plan_text}\n\n{risk_text}\n\n" + FINAL.format(h=b.horizon_hours), True)
        reports["portfolio_manager"] = final_raw
        final = parse_final(final_raw)
        plan, checks = ground(final, snap, facts)
        b.reports, b.rating, b.summary, b.plan, b.checks = reports, final["rating"], final["summary"], plan, checks
        b.status, b.progress = Brief.Status.DONE, "Done"
    except (llm.LLMError, BriefError) as exc:
        b.status, b.error, b.progress = Brief.Status.FAILED, str(exc), "Failed"
    except Exception as exc:  # noqa: BLE001 - recorded on the brief
        log.exception("Brief %s failed", b.pk)
        b.status, b.error, b.progress = Brief.Status.FAILED, f"{exc.__class__.__name__}: {exc}", "Failed"
    agg = LLMCall.objects.filter(purpose__startswith=f"brief#{b.pk} ", cached=False).aggregate(n=Count("id"), c=Sum("cost_usd"))
    b.calls, b.cost_usd = agg["n"] or 0, agg["c"] or 0.0
    b.finished_at = timezone.now()
    b.save()
    return b


# -- queue --------------------------------------------------------------------------------------------


def create(symbol: str, quote: str, exchange: str, coin=None) -> Brief:
    if not llm.enabled():
        raise llm.NotConfigured("No LLM provider configured")
    if exchange not in public.EXCHANGES:
        exchange = "gate"
    return Brief.objects.create(symbol=symbol.upper(), quote=quote.upper(), exchange=exchange, coin=coin,
                                horizon_hours=settings.INTEL["BRIEF_HORIZON_HOURS"], progress="Waiting to start")


def _thread(pk: int) -> None:
    close_old_connections()
    try:
        run(Brief.objects.select_related("coin").get(pk=pk))
    finally:
        connection.close()


def start(pk: int) -> bool:
    """Claim a queued brief and run it in a background thread (one claim wins across processes)."""
    if not Brief.objects.filter(pk=pk, status=Brief.Status.QUEUED).update(status=Brief.Status.RUNNING,
                                                                          started_at=timezone.now()):
        return False
    threading.Thread(target=_thread, args=(pk,), daemon=True, name=f"brief-{pk}").start()
    return True


def run_queued() -> int:
    """Worker periodic task: fail briefs stuck too long, then start the oldest queued one if none is running."""
    stale = timezone.now() - timedelta(minutes=STALE_MINUTES)
    Brief.objects.filter(status=Brief.Status.RUNNING, started_at__lt=stale).update(
        status=Brief.Status.FAILED, error="Interrupted (no result after 20 minutes)", progress="Failed",
        finished_at=timezone.now())
    if Brief.objects.filter(status=Brief.Status.RUNNING).exists():
        return 0
    nxt = Brief.objects.filter(status=Brief.Status.QUEUED).order_by("created_at").first()
    return int(bool(nxt and start(nxt.pk)))
