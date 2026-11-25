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

    def market(self, pair: str, side: str, qty: float, text: str | None = None) -> Fill:
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

    def _check_minimum(self, info: dict, base_amount: float, quote_amount: float) -> None:
        """Gate rejects orders below the pair's minimum; say so before sending (verified CurrencyPair fields)."""
        if info.get("min_base_amount") and base_amount < info["min_base_amount"]:
            raise OrderError(f"Order of {base_amount:g} is below Gate.io's minimum {info['min_base_amount']:g}")
        if info.get("min_quote_amount") and quote_amount < info["min_quote_amount"]:
            raise OrderError(f"Order worth {quote_amount:.4g} is below Gate.io's minimum {info['min_quote_amount']:g}")

    def market(self, pair: str, side: str, qty: float, text: str | None = None) -> Fill:
        info = self.client.pair_info(pair) or {"price_precision": 2, "amount_precision": 6}
        last = float((self.client.get_ticker(pair) or {}).get("last") or 0)
        if side == "buy":
            # Gate market buys are sized in the quote currency
            if last <= 0:
                raise OrderError("No last price to size the market buy")
            amount = math.floor(qty * last * 100) / 100
            self._check_minimum(info, qty, amount)
        else:
            step = 10 ** info["amount_precision"]
            amount = math.floor(qty * step) / step
            self._check_minimum(info, amount, amount * last if last else float("inf"))
        if amount <= 0:
            raise OrderError("Order amount rounds to zero")
        order = self._request("POST", "/spot/orders", body={
            "currency_pair": pair, "side": side, "type": "market", "amount": f"{amount}",
            "time_in_force": "ioc", "text": text or client_id(),
        })
        oid = str(order.get("id", ""))
        if order.get("status") != "closed" and oid:
            order = self._request("GET", f"/spot/orders/{oid}", f"currency_pair={pair}")
        fill = self.fill_from(order, pair)
        if fill is None:
            raise OrderError(f"Order {oid} did not fill (status {order.get('status')}, {order.get('finish_as')})")
        return fill

    def fill_from(self, order: dict, pair: str) -> Fill | None:
        """A Fill from a Gate order object. The quantity is what actually changed hands in the coin:
        on a buy whose fee is taken in the coin (Gate's default), the coin received is the filled amount
        minus that fee, so a later sell of `qty` never asks for more than the account holds."""
        side = order.get("side")
        avg = float(order.get("avg_deal_price") or order.get("fill_price") or 0)
        filled = float(order.get("filled_amount") or 0)
        if not filled and avg:
            filled = float(order.get("filled_total") or 0) / avg
        if filled <= 0 or avg <= 0:
            return None
        base, quote = pair.upper().split("_", 1)
        fee, fc = float(order.get("fee") or 0), (order.get("fee_currency") or "").upper()
        gt_fee, point_fee = float(order.get("gt_fee") or 0), float(order.get("point_fee") or 0)
        qty = filled
        if fc == base:
            fee_quote = fee * avg
            if side == "buy":
                qty = filled - fee
        elif fc == quote:
            fee_quote = fee
        else:
            fee_quote = 0.0
        if gt_fee:
            gt = float((self.client.get_ticker(f"GT_{quote}") or {}).get("last") or 0)
            fee_quote += gt_fee * gt  # GT paid the fee: its value at today's GT price (estimate)
        if point_fee:
            fee_quote += point_fee  # 1 point is about 1 USDT
        return Fill(qty, avg, fee_quote, str(order.get("id", "")))

    def find_order(self, pair: str, text: str, since: float) -> dict | None:
        """Find an order by our own client id after a crash. Gate looks a custom id up only while the order
        is open, so finished orders are searched by their `text` in the recent list."""
        try:
            return self._request("GET", f"/spot/orders/{text}", f"currency_pair={pair}")
        except OrderError:
            pass
        rows = self._request("GET", "/spot/orders",
                             f"currency_pair={pair}&status=finished&limit=100&from={int(since) - 60}")
        return next((o for o in rows if o.get("text") == text), None)

    # -- the stop-loss on the exchange (a price-triggered order), so a stop holds even if QF-gate is down --

    def place_stop(self, pair: str, qty: float, stop: float, account: str = "normal") -> str:
        info = self.client.pair_info(pair) or {"price_precision": 2, "amount_precision": 6}
        step = 10 ** info["amount_precision"]
        amount = math.floor(qty * step) / step
        price = f"{stop:.{info['price_precision']}f}"
        body = {"market": pair, "trigger": {"price": price, "rule": "<=", "expiration": 30 * 86400},
                "put": {"type": "market", "side": "sell", "price": price, "amount": f"{amount}",
                        "account": account, "time_in_force": "ioc"}}
        try:
            r = self._request("POST", "/spot/price_orders", body=body)
        except OrderError as exc:
            if account == "normal" and "unified" in str(exc).lower():
                return self.place_stop(pair, qty, stop, account="unified")  # unified accounts need "unified"
            raise
        return str(r.get("id_string") or r.get("id"))

    def stop_status(self, stop_id: str) -> dict:
        return self._request("GET", f"/spot/price_orders/{stop_id}")

    def cancel_stop(self, stop_id: str) -> dict:
        return self._request("DELETE", f"/spot/price_orders/{stop_id}")

    def get_order(self, pair: str, order_id: str) -> dict:
        return self._request("GET", f"/spot/orders/{order_id}", f"currency_pair={pair}")

    def limit(self, pair: str, side: str, amount_base: float, price: float, text: str) -> dict:
        """A resting limit order (good till cancelled), amount in the coin. Returns Gate's order object."""
        info = self.client.pair_info(pair) or {"price_precision": 2, "amount_precision": 6}
        step = 10 ** info["amount_precision"]
        amount = math.floor(amount_base * step) / step
        if amount <= 0:
            raise OrderError("Order amount rounds to zero")
        self._check_minimum(info, amount, amount * price)
        return self._request("POST", "/spot/orders", body={
            "currency_pair": pair, "side": side, "type": "limit", "amount": f"{amount}",
            "price": f"{price:.{info['price_precision']}f}", "time_in_force": "gtc", "text": text,
        })

    def cancel_order(self, pair: str, order_id: str) -> dict:
        """Cancel; returns the order as it ended (it may have filled, fully or partly, just before)."""
        return self._request("DELETE", f"/spot/orders/{order_id}", f"currency_pair={pair}")


def client_id(tag: str = "qf") -> str:
    """Our id for an order (Gate `text`): "t-" + at most 28 of [0-9A-Za-z_.-]."""
    return f"t-{tag}{int(time.time() * 1000)}"[:30]


def _credentials(owner):
    """The owner's Gate.io key from the API keys page (the operator may still use the .env keys)."""
    if owner is None:
        key, secret = settings.GATE.get("API_KEY"), settings.GATE.get("API_SECRET")
        return (key, secret) if key and secret else None
    from accounts.crypto import KeyStoreError
    from accounts.keys import gate_credentials

    try:
        return gate_credentials(owner)
    except KeyStoreError:
        return None


def live_allowed(owner=None) -> tuple[bool, list[str]]:
    """Every switch that must be on before a live order may be sent. Returns (ok, reasons it is not).
    1. the server allows live trading (LIVE_TRADING=true, set by the operator);
    2. the user has a Gate.io key (API keys page);
    3. the user switched live trading on (Trading settings)."""
    from .models import TradingControl

    missing = []
    if str(settings.TRADING.get("LIVE_TRADING")).lower() not in {"1", "true", "yes", "on"}:
        missing.append("LIVE_TRADING=true is not set in the server environment")
    if not _credentials(owner):
        missing.append("No Gate.io API key (add yours on the API keys page)" if owner is not None
                       else "GATE_API_KEY / GATE_API_SECRET are not set")
    if not TradingControl.for_user(owner).live_enabled:
        missing.append("Live trading is switched off in your settings")
    return not missing, missing


def get_broker(bot):
    fee = bot.risk.get("fee_rate", 0.002)
    if bot.mode == "live":
        owner = bot.config.owner if bot.config.owner_id else None
        ok, missing = live_allowed(owner)
        if not ok:
            raise OrderError("Live trading is not allowed: " + "; ".join(missing))
        key, secret = _credentials(owner)
        return GateBroker(key, secret)
    return DryRunBroker(fee)
