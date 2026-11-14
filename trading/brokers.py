"""Order execution: where a bot's buy/sell actually goes.

DryRunBroker  - no orders are sent. A market order is filled against Gate.io's *live* order book:
                the requested quantity walks the price levels (realistic slippage), and the taker
                fee is charged. Uses only public data, no API key.
GateBroker    - real orders on Gate.io spot through the signed REST API. Only used when every switch
                is on (see `live_allowed`). Use a key with trade-only permission, IP-whitelisted, and
                never with withdrawal rights. Market orders only; amounts are rounded down to Gate's
                precision for the pair.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass

from django.conf import settings

from market.exchanges.base import ExchangeError
from market.exchanges.gate import get_client


@dataclass
class Fill:
    qty: float  # base quantity filled
    avg_price: float
    fee: float  # in the quote currency
    exchange_id: str = ""


class OrderError(RuntimeError):
    pass


def _walk(levels: list[tuple[str, str]], qty: float) -> tuple[float, float]:
    """Fill `qty` against book levels best-first. Returns (filled qty, average price)."""
    left, cost = qty, 0.0
    for price, amount in levels:
        p, a = float(price), float(amount)
        take = min(left, a)
        cost += take * p
        left -= take
        if left <= 1e-15:
            break
    filled = qty - max(left, 0.0)
    if filled <= 0:
        raise OrderError("Order book is empty")
    if left > 1e-12:  # deeper than the book we fetched: price the rest at the last level we saw
        cost += left * float(levels[-1][0])
        filled = qty
    return filled, cost / filled


class DryRunBroker:
    name = "dry_run"

    def __init__(self, fee_rate: float, client=None, depth: int = 50):
        self.fee_rate = fee_rate
        self.client = client or get_client()
        self.depth = depth

    def market(self, pair: str, side: str, qty: float) -> Fill:
        try:
            book = self.client.get_order_book(pair, limit=self.depth)
        except ExchangeError as exc:
            raise OrderError(f"Order book unavailable: {exc}") from exc
        levels = book["asks"] if side == "buy" else book["bids"]
        if not levels:
            raise OrderError("Order book is empty")
        filled, avg = _walk(levels, qty)
        return Fill(filled, avg, filled * avg * self.fee_rate, exchange_id=f"dry-{int(time.time() * 1000)}")


def sign(secret: str, method: str, path: str, query: str, body: str, ts: str) -> str:
    """Gate.io API v4 signature: HMAC-SHA512 over method, path, query, SHA512(body) and timestamp."""
    payload = "\n".join([method.upper(), path, query, hashlib.sha512(body.encode()).hexdigest(), ts])
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()


class GateBroker:
    name = "live"
    API_PREFIX = "/api/v4"

    def __init__(self, key: str, secret: str, session=None, base_url: str | None = None, client=None):
        import requests

        if not key or not secret:
            raise OrderError("GATE_API_KEY / GATE_API_SECRET are not set")
        self.key, self.secret = key, secret
        self.base = (base_url or settings.GATE["REST_BASE"]).rstrip("/")
        self.session = session or requests.Session()
        self.client = client or get_client()

    def _request(self, method: str, path: str, query: str = "", body: dict | None = None) -> dict:
        data = json.dumps(body) if body is not None else ""
        ts = str(int(time.time()))
        headers = {"KEY": self.key, "Timestamp": ts, "Accept": "application/json", "Content-Type": "application/json",
                   "SIGN": sign(self.secret, method, self.API_PREFIX + path, query, data, ts)}
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        resp = self.session.request(method, url, data=data or None, headers=headers, timeout=15)
        if not resp.ok:
            raise OrderError(f"Gate.io {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def balance(self, currency: str) -> float:
        rows = self._request("GET", "/spot/accounts", f"currency={currency.upper()}")
        return float(rows[0]["available"]) if rows else 0.0

    def market(self, pair: str, side: str, qty: float) -> Fill:
        info = self.client.pair_info(pair) or {"price_precision": 2, "amount_precision": 6}
        if side == "buy":
            # Gate market buys are sized in the quote currency
            last = float((self.client.get_ticker(pair) or {}).get("last") or 0)
            if last <= 0:
                raise OrderError("No last price to size the market buy")
            amount = math.floor(qty * last * 100) / 100
        else:
            step = 10 ** info["amount_precision"]
            amount = math.floor(qty * step) / step
        if amount <= 0:
            raise OrderError("Order amount rounds to zero")
        order = self._request("POST", "/spot/orders", body={
            "currency_pair": pair, "side": side, "type": "market", "amount": f"{amount}",
            "time_in_force": "ioc", "text": f"t-qfgate{int(time.time())}"[:28],
        })
        oid = str(order.get("id", ""))
        if order.get("status") != "closed" and oid:
            order = self._request("GET", f"/spot/orders/{oid}", f"currency_pair={pair}")
        avg = float(order.get("avg_deal_price") or order.get("fill_price") or 0)
        filled_total = float(order.get("filled_total") or 0)
        filled = filled_total / avg if avg else 0.0
        if filled <= 0:
            raise OrderError(f"Order {oid} did not fill (status {order.get('status')})")
        fee = float(order.get("fee") or 0)
        if order.get("fee_currency") and order["fee_currency"] != pair.split("_")[1]:
            fee *= avg  # fee charged in the base currency: express it in the quote currency
        return Fill(filled, avg, fee, oid)


def live_allowed() -> tuple[bool, list[str]]:
    """Every switch that must be on before a live bot may start. Returns (ok, reasons it is not)."""
    from .models import TradingControl

    missing = []
    if str(settings.TRADING.get("LIVE_TRADING")).lower() not in {"1", "true", "yes", "on"}:
        missing.append("LIVE_TRADING=true is not set in the server environment")
    if not (settings.GATE.get("API_KEY") and settings.GATE.get("API_SECRET")):
        missing.append("GATE_API_KEY / GATE_API_SECRET are not set")
    if not TradingControl.get().live_enabled:
        missing.append("Live trading is switched off on the Control page")
    return not missing, missing


def get_broker(bot):
    fee = bot.risk.get("fee_rate", 0.002)
    if bot.mode == "live":
        ok, missing = live_allowed()
        if not ok:
            raise OrderError("Live trading is not allowed: " + "; ".join(missing))
        return GateBroker(settings.GATE["API_KEY"], settings.GATE["API_SECRET"])
    return DryRunBroker(fee)
