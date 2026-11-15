"""Fetch a coin's details automatically from its symbol.

Gate.io (authoritative for identity): name, the currency's own primary chain and contract, every chain
it supports, and whether the spot pair trades. CoinGecko (public API, no key; enrichment only): icon,
the coin's contracts on other chains and the exchanges listing it. CoinGecko is matched by contract
address for tokens (never by ticker alone) and by name + exact symbol for native coins.

Results are cached; CoinGecko failures (rate limits, outages) never block adding a coin.
"""

from __future__ import annotations

import logging

import requests
from django.core.cache import cache

from ..exchanges.base import ExchangeError
from ..exchanges.gate import get_client

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
CACHE_SECONDS = 6 * 3600
MAX_EXCHANGES = 60

# Gate.io chain code -> CoinGecko asset platform id (the common ones)
GATE_TO_CG = {
    "ETH": "ethereum", "BSC": "binance-smart-chain", "SOL": "solana", "MATIC": "polygon-pos", "POL": "polygon-pos",
    "ARBEVM": "arbitrum-one", "OPETH": "optimistic-ethereum", "BASEEVM": "base", "AVAX_C": "avalanche",
    "TRX": "tron", "TON": "the-open-network", "GRAM": "the-open-network", "SUI": "sui", "APT": "aptos",
    "NEAR": "near-protocol", "CELO": "celo", "KAVAEVM": "kava", "OPBNB": "opbnb", "LINEAETH": "linea",
    "SCROLL": "scroll", "MANTLE": "mantle", "BLAST": "blast",
}
COIN_FIELDS = {"localization": "false", "tickers": "true", "market_data": "false", "community_data": "false",
               "developer_data": "false", "sparkline": "false"}


def gate_details(symbol: str, quote: str = "USDT", client=None) -> dict | None:
    """Gate.io facts for a currency code; None if Gate.io does not list it."""
    client = client or get_client()
    code = symbol.strip().upper()
    raw = client._get(f"/spot/currencies/{code}")
    if not raw:
        return None
    chains = [{"chain": c.get("name", ""), "contract": c.get("addr") or "",
               "withdraw_disabled": bool(c.get("withdraw_disabled")), "deposit_disabled": bool(c.get("deposit_disabled"))}
              for c in raw.get("chains", []) if c.get("name")]
    primary = raw.get("chain") or (chains[0]["chain"] if chains else "")
    main = next((c for c in chains if c["chain"] == primary), {"contract": ""})
    # the primary chain first, then chains that work, then the rest
    chains.sort(key=lambda c: (c["chain"] != primary, c["withdraw_disabled"] or c["deposit_disabled"], c["chain"]))
    pair = f"{code}_{quote.upper()}"
    try:
        status = client.pair_status(pair)
    except ExchangeError:
        status = None
    return {
        "currency": raw.get("currency", code), "name": raw.get("name", ""), "delisted": bool(raw.get("delisted")),
        "chain": primary, "contract": main["contract"], "native": not main["contract"], "chains": chains,
        "pair": pair, "pair_status": status,
    }


def _cg_get(path: str, params: dict | None = None, session=None) -> dict | None:
    http = session or requests
    try:
        r = http.get(f"{CG}{path}", params=params, timeout=15,
                     headers={"Accept": "application/json", "User-Agent": "qf-gate/1.0"})
    except requests.RequestException as exc:
        log.info("CoinGecko unreachable: %s", exc)
        return None
    if r.status_code == 429:
        log.info("CoinGecko rate limit reached")
        return None
    return r.json() if r.ok else None


def coingecko_details(name: str, symbol: str, chain: str, contract: str, session=None) -> dict | None:
    """Icon, other-chain contracts and listing exchanges from CoinGecko (None if not found / unavailable)."""
    data = None
    platform = GATE_TO_CG.get(chain.upper())
    if contract and platform:
        addr = contract.lower() if contract.startswith("0x") else contract
        data = _cg_get(f"/coins/{platform}/contract/{addr}", session=session)
    elif not contract:  # native coin: search the name, keep the exact symbol, best ranked
        found = _cg_get("/search", {"query": name or symbol}, session=session) or {}
        hits = [c for c in found.get("coins", []) if c.get("symbol", "").upper() == symbol.upper()]
        hits.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
        if hits:
            data = _cg_get(f"/coins/{hits[0]['id']}", COIN_FIELDS, session=session)
    if not data or not data.get("id") or data.get("error"):
        return None
    exchanges = sorted({t["market"]["name"] for t in data.get("tickers", []) if t.get("market", {}).get("name")})
    image = data.get("image") or {}
    return {
        "id": data["id"], "name": data.get("name", ""), "image": image.get("small") or image.get("thumb") or "",
        "platforms": {k: v for k, v in (data.get("platforms") or {}).items() if k and v},
        "exchanges": exchanges[:MAX_EXCHANGES], "exchange_count": len(exchanges),
    }


def autofill(symbol: str, quote: str = "USDT", client=None, session=None) -> dict:
    """Everything the Add-coin form can fill in from a symbol."""
    key = f"coininfo:{symbol.upper()}:{quote.upper()}"
    use_cache = client is None and session is None
    if use_cache and (cached := cache.get(key)) is not None:
        return cached
    gate = gate_details(symbol, quote, client)
    if gate is None:
        return {"found": False, "symbol": symbol.upper(), "error": f"Gate.io does not list {symbol.upper()}."}
    cg = coingecko_details(gate["name"], gate["currency"], gate["chain"], gate["contract"], session=session)
    result = {"found": True, "symbol": gate["currency"], "gate": gate, "coingecko": cg}
    if use_cache and cg is not None:  # don't cache a CoinGecko miss (often just a rate limit)
        cache.set(key, result, CACHE_SECONDS)
    return result


def enrich(coin, client=None, session=None) -> bool:
    """Store icon, CoinGecko id, exchanges and Gate chains on a saved coin. True if Gate.io knew the coin."""
    info = autofill(coin.gate_currency or coin.symbol, coin.quote, client=client, session=session)
    if not info.get("found"):
        return False
    gate, cg = info["gate"], info.get("coingecko") or {}
    fields = {"chains": gate["chains"]}
    if cg:
        fields.update(image_url=cg.get("image", "")[:300], coingecko_id=cg.get("id", "")[:100],
                      exchanges=cg.get("exchanges", []))
    if not coin.name and gate.get("name"):
        fields["name"] = gate["name"][:128]
    type(coin).objects.filter(pk=coin.pk).update(**fields)
    for k, v in fields.items():
        setattr(coin, k, v)
    return True
