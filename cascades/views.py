from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render  # noqa: F401

from accounts.access import get_owned, owned
from django.views.decorators.http import require_POST

from . import backtest as bt_run
from . import engine
from .engine import Conflict
from .forms import CascadeBacktestForm, CascadeForm
from .models import Cascade, CascadeBacktest, CascadeLevel


def _initial(request) -> dict:
    """Prefill a form from ?coin=, ?from_backtest= or ?from_cascade= (same settings, other use)."""
    init: dict = {}
    src = None
    if request.GET.get("from_backtest"):
        src = owned(CascadeBacktest.objects.all(), request.user).filter(pk=request.GET["from_backtest"]).first()
    elif request.GET.get("from_cascade"):
        src = owned(Cascade.objects.all(), request.user).filter(pk=request.GET["from_cascade"]).first()
    if src is not None:
        init.update(src.config or {}, coin=src.coin_id, kind=src.kind, trigger_price=src.trigger_price,
                    fee_pct=src.fee_pct, name=src.name)
        if isinstance(src, CascadeBacktest):
            init.update(interval=src.interval, start=src.start.date(), end=src.end.date())
    elif request.GET.get("coin"):
        init["coin"] = request.GET["coin"]
    return init


def _preview(levels: list[dict]) -> dict:
    rows, total = [], 0.0
    for lv in levels:
        total += lv["amount_usdt"]
        rows.append({**lv, "sell": lv["target_price"] * (1 + lv["take_profit_pct"] / 100), "cum": total})
    return {"rows": rows, "total": total,
            "depth_pct": (levels[-1]["target_price"] / levels[0]["target_price"] - 1) * 100 if levels else 0}


@login_required
def index(request):
    cascades = list(owned(Cascade.objects.select_related("coin").prefetch_related("levels"), request.user)[:100])
    for c in cascades:
        c.t = c.totals()
    return render(request, "cascades/index.html", {
        "cascades": cascades, "backtests": owned(CascadeBacktest.objects.select_related("coin"), request.user)[:50],
    })


@login_required
def create(request):
    form = CascadeForm(request.POST or None, initial=_initial(request))
    preview = price = None
    if request.method == "POST" and form.is_valid():
        coin = form.cleaned_data["coin"]
        price = engine.live_price(coin)
        if request.POST.get("action") == "preview":
            preview = _preview(form.levels())
        else:
            try:
                c = engine.create(coin, form.cleaned_data["kind"], form.levels(),
                                  trigger_price=form.cleaned_data.get("trigger_price"), fee_pct=form.cleaned_data["fee_pct"],
                                  config=form.config(), name=form.cleaned_data.get("name") or "", price=price,
                                  owner=request.user, mode=form.cleaned_data.get("mode") or "dry_run")
            except ValueError as exc:
                form.add_error(None, str(exc))
                preview = _preview(form.levels())
            else:
                if price is None:
                    messages.warning(request, "No live price from Gate.io right now: level 1 is placed on the next step.")
                messages.success(request, f"{'LIVE' if c.mode == 'live' else 'Dry-run'} cascade #{c.pk} started."
                                 + (" Its orders are real limit orders on Gate.io." if c.mode == "live" else ""))
                return redirect("cascades:detail", pk=c.pk)
    return render(request, "cascades/form.html", {"form": form, "preview": preview, "price": price, "mode": "create"})


@login_required
def detail(request, pk: int):
    c = get_owned(Cascade.objects.select_related("coin"), request.user, pk=pk)
    levels = list(c.levels.all())
    for lv in levels:
        lv.sell_preview = lv.sell_target or lv.target_price * (1 + lv.take_profit_pct / 100)
    return render(request, "cascades/detail.html", {
        "c": c, "levels": levels, "t": c.totals(), "events": c.events.all()[:200],
    })


@login_required
@require_POST
def action(request, pk: int, what: str):
    c = get_owned(Cascade.objects.select_related("coin"), request.user, pk=pk)
    try:
        if what == "hold":
            engine.hold(c)
            messages.success(request, "On hold.")
        elif what == "resume":
            engine.resume(c, engine.live_price(c.coin))
            messages.success(request, "Resumed.")
        elif what == "cancel":
            engine.cancel(c)
            messages.success(request, "Cancelled.")
        elif what == "step":
            price = engine.live_price(c.coin)
            if price is None:
                messages.error(request, "No live price from Gate.io right now.")
            else:
                n = engine.step(c.pk, price)
                messages.success(request, f"Checked at {engine.fmt(price)}: {n} new event(s).")
        elif what == "delete":
            c.delete()
            messages.success(request, f"Cascade #{pk} deleted.")
            return redirect("cascades:index")
        else:
            messages.error(request, "Unknown action.")
    except (ValueError, Conflict) as exc:
        messages.error(request, str(exc))
    return redirect("cascades:detail", pk=pk)


def _num(request, name: str) -> float | None:
    v = (request.POST.get(name) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"{name.replace('_', ' ')} must be a number") from None


@login_required
@require_POST
def level(request, pk: int, level_pk: int, what: str):
    lv = get_owned(CascadeLevel.objects.select_related("cascade__coin"), request.user, pk=level_pk, cascade_id=pk)
    try:
        if not lv.cascade.is_running and lv.cascade.status != Cascade.Status.HELD:
            raise ValueError("This cascade has finished")
        if what == "cancel":
            engine.cancel_level(lv, request.POST.get("side", ""))
            messages.success(request, f"Level {lv.index}: order withdrawn.")
        elif what == "edit":
            engine.edit_level(lv, target_price=_num(request, "target_price"), amount_usdt=_num(request, "amount_usdt"),
                              take_profit_pct=_num(request, "take_profit_pct"), sell_target=_num(request, "sell_target"))
            messages.success(request, f"Level {lv.index} updated.")
        else:
            messages.error(request, "Unknown action.")
    except (ValueError, Conflict) as exc:
        messages.error(request, str(exc))
    return redirect("cascades:detail", pk=pk)


@login_required
def backtest_create(request):
    form = CascadeBacktestForm(request.POST or None, initial=_initial(request))
    preview = None
    if request.method == "POST" and form.is_valid():
        if request.POST.get("action") == "preview":
            preview = _preview(form.levels())
        else:
            start, end = form.period()
            bt = bt_run.run(form.cleaned_data["coin"], form.cleaned_data["kind"], form.levels(),
                            interval=form.cleaned_data["interval"], start=start, end=end,
                            trigger_price=form.cleaned_data.get("trigger_price"), fee_pct=form.cleaned_data["fee_pct"],
                            config=form.config(), name=form.cleaned_data.get("name") or "", owner=request.user)
            return redirect("cascades:backtest_detail", pk=bt.pk)
    return render(request, "cascades/form.html", {"form": form, "preview": preview, "mode": "backtest"})


@login_required
def backtest_detail(request, pk: int):
    bt = get_owned(CascadeBacktest.objects.select_related("coin"), request.user, pk=pk)
    return render(request, "cascades/backtest_detail.html", {"bt": bt, "s": bt.summary or {}})


@login_required
@require_POST
def backtest_delete(request, pk: int):
    get_owned(CascadeBacktest, request.user, pk=pk).delete()
    messages.success(request, f"Cascade backtest #{pk} deleted.")
    return redirect("cascades:index")
