from __future__ import annotations

import csv
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import gate_sync, recorder, reports
from .forms import AccountForm, CostPriceForm, FilterForm, TransactionForm
from .models import Account, BalanceCheck, Transaction
from .pnl import DEFAULT_METHOD, METHODS
from .prices import live_prices


def _refresh(user) -> None:
    try:
        recorder.ensure_accounts(user)
        recorder.record_internal()
    except Exception:  # noqa: BLE001 - the page still shows what is already recorded
        pass


def _default_scope(user) -> str:
    mine = Transaction.objects.filter(account__owner=user, account__kind=Account.Kind.REAL)
    return "real" if mine.exists() else "sim"


def _filters(request) -> tuple[FilterForm, dict]:
    data = request.GET.copy()
    data.setdefault("scope", _default_scope(request.user))
    data.setdefault("method", DEFAULT_METHOD)
    form = FilterForm(data, user=request.user)
    form.is_valid()
    d = dict(getattr(form, "cleaned_data", {}))
    d.setdefault("scope", data["scope"])
    d["method"] = d.get("method") or DEFAULT_METHOD
    return form, d


def _scoped(scope: str, user):
    qs = Transaction.objects.select_related("account", "coin").filter(account__owner=user)
    if scope == "real":
        return qs.filter(account__kind=Account.Kind.REAL)
    if scope == "sim":
        return qs.filter(account__kind=Account.Kind.SIMULATED)
    return qs.filter(account_id=int(scope)) if str(scope).isdigit() else qs.none()


def _scope_label(scope: str, user=None) -> str:
    if scope == "real":
        return "All real-money accounts"
    if scope == "sim":
        return "All simulated accounts"
    acc = Account.objects.filter(pk=scope, owner=user).first() if str(scope).isdigit() else None
    return acc.name if acc else "?"


@login_required
def index(request):
    _refresh(request.user)
    form, f = _filters(request)
    txs = list(_scoped(f["scope"], request.user))
    h = reports.holdings(txs, f["method"], live_prices())
    gate = Account.objects.filter(sync=Account.Sync.GATE, owner=request.user).first()
    return render(request, "ledger/index.html", {
        "form": form, "f": f, "scope_label": _scope_label(f["scope"], request.user), "h": h, "count": len(txs),
        "open_rows": [r for r in h["rows"] if r["open"]], "closed_rows": [r for r in h["rows"] if not r["open"]],
        "methods": METHODS, "method_label": METHODS[f["method"]], "configured": gate_sync.configured(request.user), "gate": gate,
        "check": BalanceCheck.objects.filter(account=gate).first() if gate else None,
        "accounts": Account.objects.filter(owner=request.user),
    })


def _filtered(f: dict, user):
    qs = _scoped(f["scope"], user)
    if f.get("asset"):
        qs = qs.filter(base__iexact=f["asset"].strip())
    for name in ("side", "route", "kind"):
        if f.get(name):
            qs = qs.filter(**{name: f[name]})
    return qs


def _csv_response(filename: str) -> tuple[HttpResponse, csv.writer]:
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.write("﻿")  # Excel reads UTF-8 correctly with a BOM
    return resp, csv.writer(resp)


TX_HEADER = ["time_utc", "account", "route", "source", "kind", "market", "side", "coin", "quote", "qty", "price",
             "amount_quote", "fee", "fee_currency", "fee_value_quote", "estimated", "order_id", "source_key", "note"]


def _tx_row(t: Transaction) -> list:
    def s(x):
        return "" if x is None else (format(x.normalize(), "f") if isinstance(x, Decimal) else x)

    return [t.executed_at.strftime("%Y-%m-%d %H:%M:%S"), t.account.name, t.route, t.source, t.kind, t.market, t.side,
            t.base, t.quote, s(t.qty), s(t.price), s(t.amount_quote), s(t.fee), t.fee_currency, s(t.fee_quote),
            "yes" if t.estimated else "", t.order_id, t.source_key, t.note]


@login_required
def transactions(request):
    _refresh(request.user)
    form, f = _filters(request)
    qs = _filtered(f, request.user)
    start, end = form.period() if form.is_valid() else (None, None)
    if start:
        qs = qs.filter(executed_at__gte=start)
    if end:
        qs = qs.filter(executed_at__lt=end)
    if request.GET.get("format") == "csv":
        resp, w = _csv_response("qfgate-transactions.csv")
        w.writerow(TX_HEADER)
        for t in qs.order_by("executed_at", "id").iterator():
            w.writerow(_tx_row(t))
        return resp
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "ledger/transactions.html", {
        "form": form, "f": f, "page": page, "query": query.urlencode(), "scope_label": _scope_label(f["scope"], request.user),
    })


@login_required
def report(request):
    _refresh(request.user)
    form, f = _filters(request)
    start, end = form.period() if form.is_valid() else (None, None)
    rep = reports.period(_scoped(f["scope"], request.user), f["method"], start, end, f.get("asset") or "")
    if request.GET.get("format") == "csv":
        resp, w = _csv_response("qfgate-pnl-report.csv")
        w.writerow(["P&L report", _scope_label(f["scope"], request.user), METHODS[f["method"]],
                    f"from {f.get('start') or 'start'}", f"to {f.get('end') or 'now'} (UTC)"])
        w.writerow(["market", "coin", "quote", "buys", "bought_qty", "spent", "avg_buy", "sells", "sold_qty", "received",
                    "avg_sell", "deposited_qty", "withdrawn_qty", "net_qty", "fees", "realised_pnl"])
        for r in rep["rows"]:
            w.writerow([r["market"], r["asset"], r["quote"], r.get("buys", 0), r.get("bought_qty", 0), r.get("spent", 0),
                        r.get("avg_buy") or "", r.get("sells", 0), r.get("sold_qty", 0), r.get("received", 0),
                        r.get("avg_sell") or "", r.get("in_qty", 0), r.get("out_qty", 0), r.get("net_qty", 0),
                        r.get("fees", 0), r.get("realised", 0)])
        w.writerow([])
        w.writerow(TX_HEADER)
        for t in rep["transactions"]:
            w.writerow(_tx_row(t))
        return resp
    return render(request, "ledger/report.html", {
        "form": form, "f": f, "rep": rep, "start": start, "end": end, "scope_label": _scope_label(f["scope"], request.user),
        "methods": METHODS,
    })


@login_required
def tx_new(request):
    recorder.ensure_accounts(request.user)
    form = TransactionForm(request.POST or None, user=request.user, initial={"kind": "trade", "market": "spot", "side": "buy",
                                                          "quote": "USDT", "account": request.GET.get("account")})
    if request.method == "POST" and form.is_valid():
        tx = form.save()
        messages.success(request, f"Recorded: {tx}")
        return redirect(f"{_url('ledger:transactions')}?scope={tx.account_id}")
    return render(request, "ledger/tx_form.html", {"form": form, "tx": None})


def _url(name, *args):
    from django.urls import reverse

    return reverse(name, args=args)


@login_required
def tx_edit(request, pk: int):
    tx = get_object_or_404(Transaction, pk=pk, account__owner=request.user)
    if tx.route == Transaction.Route.MANUAL or tx.source_key.startswith("manual:"):
        form = TransactionForm(request.POST or None, instance=tx, user=request.user)
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect(f"{_url('ledger:transactions')}?scope={tx.account_id}")
        return render(request, "ledger/tx_form.html", {"form": form, "tx": tx})
    if tx.kind == Transaction.Kind.TRADE:
        messages.error(request, "Recorded fills can't be edited: they are the exact record from their source.")
        return redirect(f"{_url('ledger:transactions')}?scope={tx.account_id}")
    form = CostPriceForm(request.POST or None, initial={"price": tx.price, "note": tx.note})
    if request.method == "POST" and form.is_valid():
        tx.price, tx.note = form.cleaned_data["price"], form.cleaned_data["note"]
        tx.estimated = False
        tx.save(update_fields=["price", "note", "estimated"])
        messages.success(request, "Cost price saved.")
        return redirect(f"{_url('ledger:transactions')}?scope={tx.account_id}")
    return render(request, "ledger/tx_form.html", {"form": form, "tx": tx, "cost_only": True})


@login_required
@require_POST
def tx_delete(request, pk: int):
    tx = get_object_or_404(Transaction, pk=pk, account__owner=request.user)
    if not tx.source_key.startswith("manual:"):
        messages.error(request, "Only manual entries can be deleted.")
    else:
        tx.delete()
        messages.success(request, "Deleted the manual entry.")
    return redirect(f"{_url('ledger:transactions')}?scope={tx.account_id}")


@login_required
def accounts(request):
    recorder.ensure_accounts(request.user)
    form = AccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        acc = form.save(commit=False)
        acc.owner = request.user
        acc.save()
        messages.success(request, f"Account {acc.name} added.")
        return redirect("ledger:accounts")
    rows = []
    for a in Account.objects.filter(owner=request.user).prefetch_related("sync_states"):
        rows.append({"a": a, "count": a.transactions.count(), "states": list(a.sync_states.all()),
                     "check": a.balance_checks.first()})
    return render(request, "ledger/accounts.html", {"form": form, "rows": rows,
                                                    "configured": gate_sync.configured(request.user)})


@login_required
def account_edit(request, pk: int):
    acc = get_object_or_404(Account, pk=pk, owner=request.user)
    form = AccountForm(request.POST or None, instance=acc)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect("ledger:accounts")
    return render(request, "ledger/account_form.html", {"form": form, "acc": acc})


@login_required
@require_POST
def account_action(request, pk: int, what: str):
    acc = get_object_or_404(Account, pk=pk, owner=request.user)
    if not gate_sync.configured(request.user):
        messages.error(request, "Add your Gate.io API key first (Account → API keys; a read-only key is enough).")
        return redirect("ledger:accounts")
    try:
        if what == "sync":
            n = gate_sync.sync_account(acc, max_windows=12)
            messages.success(request, f"Imported {n} new row(s). Older history continues on the next runs (every 5 min).")
        elif what == "check":
            chk = gate_sync.check_balances(acc)
            (messages.success if chk.ok else messages.warning)(request, chk.message)
    except gate_sync.SyncError as exc:
        messages.error(request, str(exc))
    return redirect("ledger:accounts")
