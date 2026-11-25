"""Import the real Gate.io account's history into the ledger, and check it against the real balances.

Gate.io only hands history out in pieces (verified in the API v4 docs, Nov 2026):
- GET /spot/my_trades: a query may not span more than 30 days; with no dates only the last 7 days;
  up to 1000 rows per page.
- GET /wallet/deposits, /wallet/withdrawals: at most 30 days per query, up to 500 rows.
So each stream walks in 30-day windows: forward from the newest imported moment up to now (with an
overlap, so nothing on a boundary is missed), and backward from the oldest imported moment until the
account's `history_start`. Progress is saved per stream, so each run continues where the last stopped.
Every row is keyed by Gate's own id (trade id / deposit id / withdrawal id): importing twice is harmless.

Needs a READ-ONLY API key (spot + wallet read, no trading, no withdrawals) in GATE_READ_API_KEY /
GATE_READ_API_SECRET. The key is only read from the environment and never shown.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_tx
from django.utils import timezone as dj_tz

from market.models import Coin

from . import pnl
from .fields import to_dec
from .models import Account, BalanceCheck, SyncState, Transaction
from .prices import price_at
from .recorder import GATE, account

log = logging.getLogger(__name__)

WINDOW = timedelta(days=30) - timedelta(seconds=1)
TRADE_OVERLAP = timedelta(hours=1)
TRANSFER_OVERLAP = timedelta(days=3)  # deposits can stay pending for a while before they are DONE
STREAMS = ("spot_trades", "deposits", "withdrawals")
TOLERANCE = Decimal("0.00000001")


class SyncError(RuntimeError):
    pass


def configured(owner=None) -> bool:
    """Can this owner's account be synced? (their own key from the API keys page, or the server's read key)"""
    return credentials_for(owner) is not None


def credentials_for(owner) -> tuple[str, str] | None:
    if owner is not None:
        from accounts.crypto import KeyStoreError
        from accounts.keys import gate_credentials

        try:
            creds = gate_credentials(owner)
        except KeyStoreError:
            creds = None
        if creds:
            return creds
    cfg = getattr(settings, "LEDGER", {})
    if cfg.get("GATE_KEY") and cfg.get("GATE_SECRET"):
        from accounts.access import operator

        op = operator()
        if owner is None or (op is not None and owner.pk == op.pk):
            return cfg["GATE_KEY"], cfg["GATE_SECRET"]
    return None


def client_for(owner) -> "ReadClient":
    creds = credentials_for(owner)
    if not creds:
        raise SyncError("No Gate.io API key: add yours on Account → API keys.")
    return ReadClient(*creds)


class ReadClient:
    """Signed GET requests only. There is no method here that can place an order or move funds."""

    API_PREFIX = "/api/v4"

    def __init__(self, key: str | None = None, secret: str | None = None, session=None, base_url: str | None = None):
        import requests

        cfg = getattr(settings, "LEDGER", {})
        self.key, self.secret = key or cfg.get("GATE_KEY", ""), secret or cfg.get("GATE_SECRET", "")
        if not self.key or not self.secret:
            raise SyncError("GATE_READ_API_KEY / GATE_READ_API_SECRET are not set")
        self.base = (base_url or settings.GATE["REST_BASE"]).rstrip("/")
        self.session = session or requests.Session()

    def get(self, path: str, params: dict | None = None):
        from urllib.parse import urlencode

        from trading.brokers import sign

        query = urlencode(params or {})
        ts = str(int(time.time()))
        headers = {"KEY": self.key, "Timestamp": ts, "Accept": "application/json",
                   "SIGN": sign(self.secret, "GET", self.API_PREFIX + path, query, "", ts)}
        resp = self.session.get(f"{self.base}{path}" + (f"?{query}" if query else ""), headers=headers, timeout=20)
        if not resp.ok:
            raise SyncError(f"Gate.io GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


# -- fetching ------------------------------------------------------------------------------------

def fetch_trades(client, start: datetime, end: datetime) -> list[dict]:
    out, page = [], 1
    while page <= 100:
        rows = client.get("/spot/my_trades", {"from": int(start.timestamp()), "to": int(end.timestamp()),
                                              "limit": 1000, "page": page})
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
        time.sleep(0.1)
    return out


def _fetch_transfers(client, path: str, start: datetime, end: datetime) -> list[dict]:
    out, offset = [], 0
    while offset < 100_000:
        rows = client.get(path, {"from": int(start.timestamp()), "to": int(end.timestamp()),
                                 "limit": 500, "offset": offset})
        out.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
        time.sleep(0.1)
    return out


def fetch_deposits(client, start, end):
    return _fetch_transfers(client, "/wallet/deposits", start, end)


def fetch_withdrawals(client, start, end):
    return _fetch_transfers(client, "/wallet/withdrawals", start, end)


FETCH = {"spot_trades": fetch_trades, "deposits": fetch_deposits, "withdrawals": fetch_withdrawals}


# -- turning Gate rows into ledger rows -------------------------------------------------------------

def _coins() -> dict[str, Coin]:
    return {c.pair: c for c in Coin.objects.all()}


def _bot_orders() -> dict[str, str]:
    from trading.models import BotOrder

    return {o["exchange_id"]: f"Bot #{o['bot_id']} · {o['bot__config__name']}"
            for o in BotOrder.objects.exclude(exchange_id="").values("exchange_id", "bot_id", "bot__config__name")}


def trade_row(acc: Account, r: dict, coins: dict, bots: dict, price_lookup=price_at) -> Transaction:
    pair = r["currency_pair"].upper()
    base, quote = pair.split("_", 1)
    at = datetime.fromtimestamp(float(r.get("create_time_ms") or 0) / 1000 or float(r["create_time"]), tz=timezone.utc)
    qty, price = to_dec(r["amount"]), to_dec(r["price"])
    fee, fc = to_dec(r.get("fee") or "0"), (r.get("fee_currency") or "").upper()
    point_fee, gt_fee = to_dec(r.get("point_fee") or "0"), to_dec(r.get("gt_fee") or "0")
    estimated = False
    if not fee and point_fee:
        fee, fc = point_fee, "POINT"
    elif not fee and gt_fee:
        fee, fc = gt_fee, "GT"
    if fc == quote:
        fee_quote = fee
    elif fc == base:
        fee_quote = fee * price
    elif fc == "POINT":
        fee_quote, estimated = fee, True  # 1 point is about 1 USDT
    elif fee:
        p = price_lookup(f"{fc}_{quote}", at)
        fee_quote, estimated = (fee * p if p is not None else None), True
    else:
        fee_quote = Decimal(0)
    oid = str(r.get("order_id") or "")
    text = str(r.get("text") or "")
    route, source = order_source(oid, text, bots)
    return Transaction(
        account=acc, kind="trade", market="spot", base=base, quote=quote, coin=coins.get(pair), side=r["side"],
        qty=qty, price=price, fee=fee, fee_currency=fc, fee_quote=fee_quote, estimated=estimated, executed_at=at,
        route=route, source=source or f"Gate.io {r.get('role', '')}".strip(),
        source_key=f"gate:spot:trade:{r['id']}", order_id=oid, raw=r)


def order_source(order_id: str, text: str, bots: dict) -> tuple[str, str]:
    """Which part of QF-gate made an order, from our client id (Gate `text`) or a bot's recorded order id:
    t-c<level>b/s… = a live cascade level, t-b<bot>x… / t-qfgate… = a bot. Anything else was made on Gate.io."""
    import re

    m = re.match(r"t-c(\d+)([bs])", text)
    if m:
        from cascades.models import CascadeLevel

        lv = CascadeLevel.objects.filter(pk=int(m.group(1))).select_related("cascade").first()
        if lv is not None:
            c = lv.cascade
            name = f" · {c.name}" if c.name else ""
            return "cascade", f"Cascade #{c.pk}{name} · L{lv.index} {'buy' if m.group(2) == 'b' else 'sell'}"[:120]
        return "cascade", "Cascade order"
    if bots.get(order_id):
        return "bot", bots[order_id]
    m = re.match(r"t-b(\d+)x", text)
    if m:
        return "bot", f"Bot #{m.group(1)}"
    if text.startswith("t-qf"):
        return "bot", "QF-gate order"
    return "gate", ""


def transfer_row(acc: Account, r: dict, kind: str, price_lookup=price_at) -> Transaction | None:
    if str(r.get("status", "")).upper() != "DONE":
        return None  # pending / failed: picked up by a later run once Gate marks it DONE
    cur = r["currency"].upper()
    at = datetime.fromtimestamp(int(float(r["timestamp"])), tz=timezone.utc)
    price = None if cur == "USDT" else price_lookup(f"{cur}_USDT", at)
    fee = to_dec(r.get("fee") or "0") if kind == "withdrawal" else Decimal(0)
    return Transaction(
        account=acc, kind=kind, market="spot", base=cur, quote="USDT", side="in" if kind == "deposit" else "out",
        qty=to_dec(r["amount"]), price=price, fee=fee, fee_currency=cur if fee else "", fee_quote=None,
        estimated=price is not None, executed_at=at, route="transfer",
        source=f"{'Deposit' if kind == 'deposit' else 'Withdrawal'} {r.get('chain') or ''}".strip(),
        source_key=f"gate:{kind}:{r['id']}", raw=r,
        note="Cost = Gate.io candle close at that time (estimate)" if price is not None and kind == "deposit" else "")


def store(acc: Account, stream: str, rows: list[dict], price_lookup=price_at) -> int:
    if not rows:
        return 0
    known = set(Transaction.objects.filter(account=acc, source_key__startswith="gate:")
                .values_list("source_key", flat=True))
    coins, bots = (_coins(), _bot_orders()) if stream == "spot_trades" else ({}, {})
    new = []
    for r in rows:
        key = {"spot_trades": "gate:spot:trade:", "deposits": "gate:deposit:", "withdrawals": "gate:withdrawal:"}[stream]
        if f"{key}{r['id']}" in known:
            continue
        tx = (trade_row(acc, r, coins, bots, price_lookup) if stream == "spot_trades"
              else transfer_row(acc, r, stream[:-1], price_lookup))
        if tx is not None:
            new.append(tx)
            known.add(tx.source_key)
    with db_tx.atomic():
        Transaction.objects.bulk_create(new, ignore_conflicts=True)
        oids = {t.order_id for t in new if t.order_id}
        if oids:  # exact trades replace the provisional rows written from live bot orders
            Transaction.objects.filter(account=acc, provisional=True, order_id__in=oids).delete()
    return len(new)


# -- walking the windows -----------------------------------------------------------------------

def _start_of(acc: Account) -> datetime:
    d = acc.history_start or date.fromisoformat(getattr(settings, "LEDGER", {}).get("HISTORY_START", "2017-01-01"))
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def sync_stream(acc: Account, stream: str, client, now: datetime | None = None, max_windows: int = 12,
                price_lookup=price_at) -> int:
    now = now or dj_tz.now()
    st, _ = SyncState.objects.get_or_create(account=acc, stream=stream)
    fetch = FETCH[stream]
    added = windows = 0
    overlap = TRADE_OVERLAP if stream == "spot_trades" else TRANSFER_OVERLAP
    try:
        # forward: everything new since the last run
        cursor = (st.newest_done - overlap) if st.newest_done else now - WINDOW
        if st.oldest_done is None:
            st.oldest_done = cursor
        while cursor < now and windows < max_windows:
            end = min(cursor + WINDOW, now)
            added += store(acc, stream, fetch(client, cursor, end), price_lookup)
            st.newest_done, cursor, windows = end, end, windows + 1
        # backward: older history, a few windows per run
        floor = _start_of(acc)
        while not st.backfill_complete and windows < max_windows:
            if st.oldest_done <= floor:
                st.backfill_complete = True
                break
            start = max(st.oldest_done - WINDOW, floor)
            added += store(acc, stream, fetch(client, start, st.oldest_done), price_lookup)
            st.oldest_done, windows = start, windows + 1
        st.last_error = ""
    except Exception as exc:  # noqa: BLE001 - keep progress made so far, show the error on the page
        st.last_error = str(exc)[:500]
        log.warning("Ledger sync %s %s failed: %s", acc, stream, exc)
    st.rows += added
    st.last_run = now
    st.save()
    return added


def sync_account(acc: Account, client=None, now=None, max_windows: int = 12) -> int:
    client = client or client_for(acc.owner)
    return sum(sync_stream(acc, s, client, now, max_windows) for s in STREAMS)


def sync_all() -> int:
    """Worker task: sync every Gate-synced account whose owner has a key."""
    from accounts.models import ExchangeKey

    for k in ExchangeKey.objects.filter(is_active=True, owner__is_active=True).select_related("owner"):
        account(GATE, k.owner)  # a user with a key gets their Gate.io account even before opening Portfolio
    total = 0
    for acc in Account.objects.filter(sync=Account.Sync.GATE).select_related("owner"):
        if acc.owner is not None and not acc.owner.is_active:
            continue
        if not configured(acc.owner):
            continue
        try:
            total += sync_account(acc, max_windows=6)
        except SyncError as exc:
            log.warning("Ledger sync %s: %s", acc, exc)
    return total


# -- balance check -----------------------------------------------------------------------------

def check_balances(acc: Account, client=None) -> BalanceCheck:
    """Compare the ledger's net amount of every currency with Gate.io's real spot balance."""
    client = client or client_for(acc.owner)
    real = {}
    for r in client.get("/spot/accounts"):
        amount = to_dec(r.get("available") or "0") + to_dec(r.get("locked") or "0")
        if amount:
            real[r["currency"].upper()] = amount
    mine = {k: v for k, v in pnl.balances(acc.transactions.all()).items() if v}
    rows, ok = [], True
    for cur in sorted(set(real) | set(mine)):
        a, b = mine.get(cur, Decimal(0)), real.get(cur, Decimal(0))
        diff = a - b
        good = abs(diff) <= TOLERANCE
        ok &= good
        rows.append({"currency": cur, "ledger": str(a), "exchange": str(b), "diff": str(diff), "ok": good})
    states = list(acc.sync_states.all())
    pending = not states or not all(s.backfill_complete for s in states)
    msg = ("All balances match the ledger." if ok else
           "Some balances differ" + (": the history import is not finished yet, so differences are expected."
                                     if pending else ". Earn, convert, airdrops or futures transfers are not imported; "
                                     "add an adjustment for each to reconcile."))
    return BalanceCheck.objects.create(account=acc, ok=ok, rows=rows, message=msg[:255])


def check_all() -> int:
    n = 0
    for acc in Account.objects.filter(sync=Account.Sync.GATE).select_related("owner"):
        if not configured(acc.owner):
            continue
        try:
            check_balances(acc)
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Balance check %s failed: %s", acc, exc)
    return n


def describe(stream: str) -> str:
    return {"spot_trades": "Spot trades", "deposits": "Deposits", "withdrawals": "Withdrawals"}.get(stream, stream)


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
