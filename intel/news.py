"""7.2 News and sentiment feed (idea from TradingAgents' news / sentiment analysts; free sources, no key).

- Sources: crypto RSS feeds (CoinDesk, Cointelegraph, Decrypt by default) and Reddit r/CryptoCurrency (new posts).
- Every item is stored ONCE with `first_seen` = when we first fetched it. Decisions (briefs) only ever read
  items seen before their own time (`items_for(..., as_of=...)`), so a headline published "earlier" but
  fetched later can't leak backwards.
- A coin matches an item when its ticker appears as a whole word in capitals (SOL, $SOL) or its name
  (Solana) appears case-insensitively. Tickers are not unique, so this is a best-effort match.
- Sentiment is a deliberately simple word count (bullish vs bearish words in the title + summary): cheap,
  repeatable and explainable, not a model. It gives a rough mood, not a signal.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.utils import timezone as dj_tz

from .models import NewsItem

log = logging.getLogger(__name__)
UA = {"User-Agent": "qf-gate/1.0 (personal research app)", "Accept": "*/*"}
TIMEOUT = 15
STATUS_KEY = "intel:news:status"

BULLISH = ("surge", "soar", "rally", "rallies", "gain", "jump", "rise", "rises", "record high", "all-time high",
           "bullish", "breakout", "upgrade", "approval", "approved", "approves", "adopt", "partnership", "launch",
           "inflow", "accumulat", "rebound", "recover", "buyback", "outperform", "climb")
BEARISH = ("plunge", "crash", "drop", "fall", "falls", "slump", "decline", "bearish", "hack", "exploit", "lawsuit",
           "sue", "sues", "ban", "outflow", "liquidat", "sell-off", "selloff", "delist", "fraud", "scam", "investigat",
           "warning", "dump", "tumble", "sink", "loss", "losses", "breach", "halt", "bankrupt")
_WORD = {w: re.compile(r"(?<![a-z])" + re.escape(w)) for w in BULLISH + BEARISH}

# names for common tickers, used when the coin isn't stored (Scanner) or has no name
NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "Ripple", "ADA": "Cardano",
         "DOGE": "Dogecoin", "AVAX": "Avalanche", "DOT": "Polkadot", "LINK": "Chainlink", "LTC": "Litecoin",
         "TRX": "Tron", "TON": "Toncoin", "ARB": "Arbitrum", "OP": "Optimism", "APT": "Aptos",
         "NEAR": "NEAR Protocol", "PEPE": "Pepe", "SHIB": "Shiba Inu", "UNI": "Uniswap", "ATOM": "Cosmos",
         "HYPE": "Hyperliquid", "XLM": "Stellar", "BCH": "Bitcoin Cash", "ZEC": "Zcash"}


def score_text(text: str) -> tuple[str, float]:
    t = (text or "").lower()
    bull = sum(1 for w in BULLISH if _WORD[w].search(t))
    bear = sum(1 for w in BEARISH if _WORD[w].search(t))
    if not bull and not bear:
        return "neutral", 0.0
    s = (bull - bear) / (bull + bear)
    return ("bullish" if s > 0 else "bearish" if s < 0 else "neutral"), s


def _clean(s: str | None) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _date(s: str | None):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def parse_rss(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{uid, source, title, summary, url, published_at}]."""
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    items = list(root.iter("item")) or list(root.iter(f"{atom}entry"))
    out = []
    for it in items:
        def g(tag):
            el = it.find(tag)
            return el if el is not None else it.find(f"{atom}{tag}")

        title_el, link_el, guid_el = g("title"), g("link"), g("guid")
        desc_el = g("description") if g("description") is not None else g("summary")
        pub_el = g("pubDate") if g("pubDate") is not None else g("published")
        title = _clean(title_el.text if title_el is not None else "")
        if not title:
            continue
        link = ((link_el.text or link_el.get("href") or "") if link_el is not None else "").strip()
        guid = ((guid_el.text if guid_el is not None else "") or link or title).strip()
        out.append({"uid": hashlib.sha1(guid.encode()).hexdigest(), "source": source, "title": title[:500],
                    "summary": _clean(desc_el.text if desc_el is not None else "")[:2000], "url": link[:1000],
                    "published_at": _date(pub_el.text if pub_el is not None else None)})
    return out


def parse_reddit(data: dict) -> list[dict]:
    out = []
    for child in (data or {}).get("data", {}).get("children", []):
        p = child.get("data", {})
        if not p.get("title"):
            continue
        out.append({"uid": hashlib.sha1(f"reddit:{p.get('id')}".encode()).hexdigest(), "source": "reddit r/CryptoCurrency",
                    "title": _clean(p["title"])[:500], "summary": _clean(p.get("selftext", ""))[:2000],
                    "url": ("https://www.reddit.com" + p["permalink"]) if p.get("permalink") else "",
                    "published_at": datetime.fromtimestamp(p["created_utc"], tz=timezone.utc) if p.get("created_utc") else None})
    return out


def store(items: list[dict], now=None) -> int:
    """Save new items (first_seen = now); items already stored keep their original first_seen."""
    now = now or dj_tz.now()
    new = 0
    have = set(NewsItem.objects.filter(uid__in=[i["uid"] for i in items]).values_list("uid", flat=True))
    for i in items:
        if i["uid"] in have:
            continue
        sentiment, score = score_text(f"{i['title']} {i.get('summary', '')}")
        try:
            NewsItem.objects.create(first_seen=now, sentiment=sentiment, score=score, **i)
            new += 1
        except IntegrityError:
            pass  # fetched twice at once
        have.add(i["uid"])
    return new


def _source_name(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0][:64]


def fetch_all(session=None) -> dict:
    """Fetch every source; a source that fails is recorded and skipped. Returns {source: status}."""
    http = session or requests
    cfg = settings.INTEL
    status, now = {}, dj_tz.now()
    jobs = [(_source_name(u), "rss", u) for u in cfg["NEWS_FEEDS"]]
    if cfg["NEWS_REDDIT"]:
        jobs.append(("reddit r/CryptoCurrency", "reddit", "https://www.reddit.com/r/CryptoCurrency/new.json?limit=50"))
    for name, kind, url in jobs:
        try:
            r = http.get(url, headers=UA, timeout=TIMEOUT)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            items = parse_rss(r.text, name) if kind == "rss" else parse_reddit(r.json())
            status[name] = {"ok": True, "items": len(items), "new": store(items, now), "error": "", "at": now.isoformat()}
        except Exception as exc:  # noqa: BLE001 - one source down must not stop the others
            log.info("news source %s failed: %s", name, exc)
            status[name] = {"ok": False, "items": 0, "new": 0, "error": str(exc)[:200], "at": now.isoformat()}
    NewsItem.objects.filter(first_seen__lt=now - timedelta(days=cfg["NEWS_KEEP_DAYS"])).delete()
    cache.set(STATUS_KEY, status, None)
    return status


def source_status() -> dict:
    return cache.get(STATUS_KEY) or {}


def matcher(symbol: str, name: str = ""):
    symbol = (symbol or "").upper()
    name = (name or NAMES.get(symbol, "")).strip()
    pats = [re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(symbol) + r"(?![A-Za-z0-9])")] if symbol else []
    if len(name) >= 4 and name.upper() != symbol:
        pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.IGNORECASE))
    return lambda text: any(p.search(text or "") for p in pats)


def items_for(symbol: str, name: str = "", as_of=None, hours: int = 72, limit: int = 30) -> list[NewsItem]:
    """Items mentioning the coin, first seen in (as_of - hours, as_of]. Never returns anything seen after as_of."""
    as_of = as_of or dj_tz.now()
    match = matcher(symbol, name)
    qs = NewsItem.objects.filter(first_seen__lte=as_of, first_seen__gt=as_of - timedelta(hours=hours))
    out = []
    for it in qs.order_by("-first_seen")[:3000]:
        if match(f"{it.title} {it.summary}"):
            out.append(it)
            if len(out) >= limit:
                break
    return out


def mood(items) -> dict:
    c = {"bullish": 0, "bearish": 0, "neutral": 0}
    for it in items:
        c[it.sentiment] = c.get(it.sentiment, 0) + 1
    n = sum(c.values())
    c["total"] = n
    c["net"] = (c["bullish"] - c["bearish"]) / n if n else 0.0
    return c
