"""7.5 Macro and prediction-market panel (idea from TradingAgents' FRED / Polymarket vendors). Information only.

- FRED public CSV (no key): effective Fed funds rate, US 10-year yield, broad dollar index.
- CoinGecko global: total crypto market cap, 24h change, BTC and ETH dominance.
- Polymarket (gamma API, public): the most-traded open markets about crypto, with the "Yes" probability.

Every value carries its own date; the whole panel is cached for an hour; a source that fails shows its
error and the rest still render. Also an input to the AI brief (7.4).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone as dt_tz

import requests
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)
UA = {"User-Agent": "qf-gate/1.0 (personal research app)", "Accept": "*/*"}
TIMEOUT = 15
CACHE_KEY = "intel:macro"
CACHE_SECONDS = 3600

FRED_SERIES = {
    "DFF": "Fed funds rate (effective), %",
    "DGS10": "US 10-year Treasury yield, %",
    "DTWEXBGS": "US dollar index (broad, trade-weighted)",
}
CRYPTO_WORDS = re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|crypto|xrp|dogecoin|stablecoin)\b", re.IGNORECASE)


def parse_fred_csv(text: str) -> dict | None:
    """Last value and the value ~1 month (about 21 observations) earlier; '.' marks a missing day."""
    rows = [r for r in csv.reader(io.StringIO(text)) if len(r) >= 2]
    vals = []
    for date, v in rows[1:]:
        try:
            vals.append((date, float(v)))
        except ValueError:
            continue
    if not vals:
        return None
    date, last = vals[-1]
    prev = vals[-22][1] if len(vals) >= 22 else vals[0][1]
    return {"date": date, "value": last, "month_ago": prev, "change": last - prev}


def _fred(session) -> list[dict]:
    out = []
    for sid, label in FRED_SERIES.items():
        r = session.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid}, headers=UA, timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"FRED HTTP {r.status_code}")
        v = parse_fred_csv(r.text)
        if v:
            out.append({"id": sid, "label": label, **v})
    return out


def parse_global(d: dict) -> dict:
    g = (d or {}).get("data") or {}
    pct = g.get("market_cap_percentage") or {}
    updated = g.get("updated_at")
    return {"total_mcap_usd": (g.get("total_market_cap") or {}).get("usd"),
            "mcap_change_24h_pct": g.get("market_cap_change_percentage_24h_usd"),
            "btc_dominance": pct.get("btc"), "eth_dominance": pct.get("eth"),
            "date": datetime.fromtimestamp(updated, tz=dt_tz.utc).strftime("%Y-%m-%d %H:%M UTC") if updated else ""}


def parse_polymarket(markets: list) -> list[dict]:
    out = []
    for m in markets or []:
        q = m.get("question") or ""
        if not CRYPTO_WORDS.search(q):
            continue
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        except (ValueError, TypeError):
            continue
        if not outcomes or len(outcomes) != len(prices):
            continue
        yes = prices[outcomes.index("Yes")] if "Yes" in outcomes else prices[0]
        out.append({"question": q, "yes_pct": yes * 100, "outcome": "Yes" if "Yes" in outcomes else outcomes[0],
                    "volume_24h": float(m.get("volume24hr") or 0), "end": (m.get("endDate") or "")[:10],
                    "url": f"https://polymarket.com/event/{m['slug']}" if m.get("slug") else ""})
    return sorted(out, key=lambda x: -x["volume_24h"])[:8]


def _polymarket(session) -> list[dict]:
    r = session.get("https://gamma-api.polymarket.com/markets", headers=UA, timeout=TIMEOUT,
                    params={"active": "true", "closed": "false", "limit": 200, "order": "volume24hr", "ascending": "false"})
    if not r.ok:
        raise RuntimeError(f"Polymarket HTTP {r.status_code}")
    return parse_polymarket(r.json())


def _global(session) -> dict:
    r = session.get("https://api.coingecko.com/api/v3/global", headers=UA, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"CoinGecko HTTP {r.status_code}")
    return parse_global(r.json())


def refresh(session=None) -> dict:
    http = session or requests
    out = {"fetched_at": timezone.now().strftime("%Y-%m-%d %H:%M UTC"), "errors": {}}
    for name, fn, empty in (("fred", _fred, []), ("crypto", _global, {}), ("polymarket", _polymarket, [])):
        try:
            out[name] = fn(http)
        except Exception as exc:  # noqa: BLE001 - one source down must not hide the others
            log.info("macro source %s failed: %s", name, exc)
            out[name] = empty
            out["errors"][name] = str(exc)[:200]
    cache.set(CACHE_KEY, out, CACHE_SECONDS)
    return out


def panel(session=None) -> dict:
    return cache.get(CACHE_KEY) or refresh(session)
