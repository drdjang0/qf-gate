from __future__ import annotations

import json

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .exchanges.base import ExchangeError
from .exchanges.gate import get_client
from .forms import CoinForm
from .models import Coin, Job
from .services import chart, coininfo, compare, gaps, jobs, live, scanner, screener, zones
from .services.history import daily_history, summarize
from .services.export import build_workbook, build_zones_workbook
from .services.levels import levels_with_previous
from .services.verify import find_by_contract, verify_coin


class CoinListView(LoginRequiredMixin, ListView):
    model = Coin
    template_name = "market/coin_list.html"

    def get_queryset(self):
        return Coin.objects.annotate(last_candle=Max("candles__open_time", filter=Q(candles__interval="1m")))


class _CoinFormMixin(LoginRequiredMixin):
    model = Coin
    form_class = CoinForm
    template_name = "market/coin_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        if form.verification:
            for w in form.verification.warnings:
                messages.warning(self.request, w)
            messages.success(self.request, f"{self.object.symbol}: verified as Gate.io {self.object.pair}.")
        else:
            messages.warning(self.request, f"{self.object.symbol} saved WITHOUT Gate.io verification.")
        return response

    def get_success_url(self):
        return reverse("market:coin_detail", args=[self.object.pk])


class CoinCreateView(_CoinFormMixin, CreateView):
    def form_valid(self, form):
        response = super().form_valid(form)
        try:
            coininfo.enrich(self.object)  # icon, exchanges, chains: best effort, never blocks the save
        except Exception:  # noqa: BLE001
            pass
        return response

    def get_initial(self):
        initial = super().get_initial()
        for k in ("symbol", "gate_currency", "quote"):
            if self.request.GET.get(k):
                initial[k] = self.request.GET[k].upper()[:20]
        return initial


class CoinUpdateView(_CoinFormMixin, UpdateView):
    pass


class CoinDeleteView(LoginRequiredMixin, DeleteView):
    model = Coin
    template_name = "market/coin_confirm_delete.html"
    success_url = reverse_lazy("market:coin_list")

    def form_valid(self, form):
        messages.success(self.request, f"Deleted {self.object} and all its candles.")
        return super().form_valid(form)


@login_required
def coin_detail(request, pk: int):
    coin = get_object_or_404(Coin, pk=pk)
    last = coin.candles.filter(interval="1m").order_by("-open_time").first()
    lag = (timezone.now() - last.open_time).total_seconds() if last else None
    return render(
        request,
        "market/coin_detail.html",
        {
            "coin": coin,
            "rows": levels_with_previous(coin),
            "last_candle": last,
            "lag_seconds": lag,
            "day_tz": settings.MARKET["DAY_TIMEZONE"],
            "candle_counts": {
                i: coin.candles.filter(interval=i).count() for i in ("1m", "5m", "15m", "1h", "1d")
            },
            "recent_jobs": coin.jobs.all()[:5],
            "active_job": coin.jobs.filter(status__in=jobs.ACTIVE).first(),
            "chart_intervals": list(chart.CHART_INTERVALS),
            "chart_max_days_json": json.dumps(chart.MAX_DAYS),
            "gaps": gaps.coverage(coin),
        },
    )


@login_required
@require_POST
def coin_repair(request, pk: int):
    """Queue a sync that reaches back to the oldest partial / missing day."""
    coin = get_object_or_404(Coin, pk=pk)
    days = gaps.repair_days(gaps.coverage(coin))
    if not days:
        messages.info(request, "No gaps to repair.")
        return redirect("market:coin_detail", pk=pk)
    job, created = jobs.enqueue(Job.Kind.SYNC, coin, {"days": min(days, 3650)})
    messages.info(request, f"Repair sync queued ({days} days back)." if created else f"A sync is already {job.status}.")
    return redirect("market:coin_detail", pk=pk)


@login_required
@require_POST
def coin_sync(request, pk: int):
    """Queue a sync job; `run_worker` picks it up."""
    coin = get_object_or_404(Coin, pk=pk)
    if not coin.verified_at:
        messages.error(request, "Verify the coin's chain + contract before syncing.")
        return redirect("market:coin_detail", pk=pk)
    try:
        days = max(1, min(int(request.POST.get("days") or settings.MARKET["HISTORY_DAYS"]), 365))
    except ValueError:
        days = settings.MARKET["HISTORY_DAYS"]
    job, created = jobs.enqueue(Job.Kind.SYNC, coin, {"days": days})
    if not created:
        messages.info(request, f"A sync for {coin.pair} is already {job.status}.")
    else:
        messages.info(request, f"Sync of {coin.pair} queued ({days} days).")
    worker = jobs.service_status()["worker"]
    if not (worker and worker.is_alive):
        messages.warning(request, "The worker is not running. Start it with: python manage.py run_worker")
    return redirect("market:coin_detail", pk=pk)


CHART_CACHE_HEADERS = {"Cache-Control": "no-store"}


@login_required
@require_GET
def candles_json(request, pk: int):
    """Chart data. ?interval=15m&days=10, or &since=<unix> for incremental live updates."""
    coin = get_object_or_404(Coin, pk=pk)
    interval = request.GET.get("interval", "15m")
    if interval not in chart.CHART_INTERVALS:
        return JsonResponse({"error": f"interval must be one of {list(chart.CHART_INTERVALS)}"}, status=400)
    try:
        days = int(request.GET["days"]) if request.GET.get("days") else None
        since = int(request.GET["since"]) if request.GET.get("since") else None
        abs_start = int(request.GET["start"]) if request.GET.get("start") else None
        abs_end = int(request.GET["end"]) if request.GET.get("end") else None
    except ValueError:
        return JsonResponse({"error": "days, since, start and end must be integers (unix seconds)"}, status=400)
    start, end = chart.window(interval, days)
    if abs_start is not None:
        # explicit range (backtest / paper pages), capped to the interval's maximum span
        end = datetime.fromtimestamp(abs_end, tz=dt_timezone.utc) if abs_end else end
        max_span = timedelta(days=chart.MAX_DAYS[interval])
        start = max(datetime.fromtimestamp(abs_start, tz=dt_timezone.utc), end - max_span)
    if since is not None:
        # re-send the last bar (it may still be forming) plus anything newer
        start = max(start, datetime.fromtimestamp(since, tz=dt_timezone.utc))
    tz_name = settings.MARKET["DAY_TIMEZONE"]
    offset = int(timezone.now().astimezone(ZoneInfo(tz_name)).utcoffset().total_seconds())
    data = {
        "pair": coin.pair,
        "interval": interval,
        "tz": tz_name,
        "tz_offset": offset,
        "candles": chart.chart_candles(coin, interval, start, end),
        "levels": chart.chart_levels(coin, start - timedelta(days=1) if since else start, end),
        "server_time": int(timezone.now().timestamp()),
    }
    return JsonResponse(data, headers=CHART_CACHE_HEADERS)


@login_required
def market_page(request, pk: int | None = None):
    """Exchange-style page: live candle chart + order book + recent trades, and the day-by-day history."""
    coins = list(Coin.objects.filter(is_active=True).order_by("symbol"))
    if pk is None:
        if not coins:
            messages.info(request, "Add a coin first.")
            return redirect("market:coin_create")
        return redirect("market:market_page", pk=coins[0].pk)
    coin = get_object_or_404(Coin, pk=pk)
    if coin not in coins:
        coins.append(coin)
    try:
        info = live.pair_info(get_client(), coin.pair)
    except ExchangeError:
        info = {"price_precision": 2, "amount_precision": 4}
    history = daily_history(coin)
    return render(
        request,
        "market/market.html",
        {
            "coin": coin,
            "coins": coins,
            "history": history,
            "summary": summarize(history),
            "day_tz": settings.MARKET["DAY_TIMEZONE"],
            "chart_intervals": list(chart.CHART_INTERVALS),
            "chart_max_days_json": json.dumps(chart.MAX_DAYS),
            "groups": live.group_options(info["price_precision"]),
            "depths": live.DEPTHS,
            "default_depth": live.DEFAULT_DEPTH,
            "pair_info": info,
        },
    )


@login_required
@require_GET
def book_json(request, pk: int):
    """Live order book, 24h ticker and recent trades from Gate.io. ?depth=20&group=0"""
    coin = get_object_or_404(Coin, pk=pk)
    try:
        depth = int(request.GET.get("depth") or live.DEFAULT_DEPTH)
    except ValueError:
        depth = live.DEFAULT_DEPTH
    group = request.GET.get("group") or "0"
    try:
        allowed = {"0", *live.group_options(live.pair_info(get_client(), coin.pair)["price_precision"])}
        if group not in allowed:
            return JsonResponse({"error": f"group must be one of {sorted(allowed)}"}, status=400)
        data = live.snapshot(get_client(), coin.pair, depth, group)
    except ExchangeError as exc:
        return JsonResponse({"error": f"Gate.io unreachable: {exc}"}, status=502, headers=CHART_CACHE_HEADERS)
    return JsonResponse(data, headers=CHART_CACHE_HEADERS)


def _zone_width(request) -> float:
    try:
        w = float(request.GET.get("width") or zones.DEFAULT_WIDTH_PCT)
    except ValueError:
        w = zones.DEFAULT_WIDTH_PCT
    return round(min(max(w, zones.MIN_WIDTH_PCT), zones.MAX_WIDTH_PCT), 2)


def _zone_days(request) -> int:
    try:
        d = int(request.GET.get("days") or zones.DEFAULT_DAYS)
    except ValueError:
        d = zones.DEFAULT_DAYS
    return d if d in zones.DAY_CHOICES else zones.DEFAULT_DAYS


@login_required
def zones_page(request, pk: int):
    """Forward-only zone analysis: revisits of past daily highs/lows, hotspots and a heatmap."""
    coin = get_object_or_404(Coin, pk=pk)
    width = _zone_width(request)
    days = _zone_days(request)
    result = zones.analyse(coin, width, days=days)
    kind = request.GET.get("kind", "all")
    try:
        min_touches = max(0, int(request.GET.get("min") or 0))
    except ValueError:
        min_touches = 0
    shown = [z for z in result["zones"] if (kind == "all" or z.kind == kind) and len(z.touches) >= min_touches]
    shown.sort(key=lambda z: (z.date, z.kind), reverse=True)
    price = result["price"]
    rows = [{"zone": z, "dist": (z.price - price) / price * 100 if price else None} for z in shown]
    for h in result["hotspots"]:
        h["dist"] = ((h["lower"] + h["upper"]) / 2 - price) / price * 100 if price else None
        h["here"] = bool(price and h["lower"] <= price <= h["upper"])
    all_touches = [t for z in result["zones"] for t in z.touches]
    decided = [t for t in all_touches if t.result in ("held", "broke")]
    return render(request, "market/zones.html", {
        "coin": coin,
        "coins": list(Coin.objects.filter(is_active=True).order_by("symbol")),
        "r": result,
        "rows": rows,
        "kind": kind,
        "min_touches": min_touches,
        "width": width,
        "days": days,
        "day_choices": zones.DAY_CHOICES,
        "touched": sum(1 for z in result["zones"] if z.touches),
        "touch_total": len(all_touches),
        "held_pct": sum(t.result == "held" for t in decided) / len(decided) * 100 if decided else None,
        "bands_json": json.dumps([
            {"lower": h["lower"], "upper": h["upper"], "title": f"#{i}"} for i, h in enumerate(result["hotspots"], 1)
        ]),
        "chart_intervals": list(chart.CHART_INTERVALS),
        "chart_max_days_json": json.dumps(chart.MAX_DAYS),
        "day_tz": result["tz"],
    })


@login_required
@require_GET
def zones_export(request, pk: int):
    coin = get_object_or_404(Coin, pk=pk)
    width = _zone_width(request)
    data = build_zones_workbook(coin, zones.analyse(coin, width, days=_zone_days(request)))
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="zones-{coin.pair}-{width}pct-{timezone.now():%Y%m%d}.xlsx"'
    return resp


class JobListView(LoginRequiredMixin, ListView):
    model = Job
    template_name = "market/job_list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = Job.objects.select_related("coin")
        if self.request.GET.get("coin"):
            qs = qs.filter(coin_id=self.request.GET["coin"])
        return qs


@login_required
@require_POST
def coin_verify(request, pk: int):
    coin = get_object_or_404(Coin, pk=pk)
    try:
        res = verify_coin(get_client(), coin)
    except ExchangeError as exc:
        messages.error(request, f"Gate.io unreachable: {exc}")
    else:
        (messages.success if res.ok else messages.error)(request, res.summary)
    return redirect("market:coin_detail", pk=pk)


@login_required
@require_GET
def export_levels(request):
    coin_id = request.GET.get("coin")
    coins = [get_object_or_404(Coin, pk=coin_id)] if coin_id else list(Coin.objects.filter(is_active=True))
    data = build_workbook(coins)
    name = f"daily-levels-{coins[0].pair if coin_id else 'all'}-{timezone.now():%Y%m%d}.xlsx"
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@login_required
@require_GET
def contract_lookup(request):
    """JSON: which Gate.io currencies list this contract address (used by the coin form)."""
    address = request.GET.get("address", "").strip()
    if len(address) < 6:
        return JsonResponse({"matches": [], "error": "Enter a contract address"}, status=400)
    try:
        matches = find_by_contract(get_client(), address)
    except ExchangeError as exc:
        return JsonResponse({"matches": [], "error": str(exc)}, status=502)
    return JsonResponse(
        {"matches": [{"currency": c.currency, "name": c.name, "chain": ch, "delisted": c.delisted} for c, ch in matches]}
    )


def _num(request, name: str, default: float) -> float:
    try:
        return float(request.GET.get(name) or default)
    except ValueError:
        return default


@login_required
def screener_page(request):
    """Shortlist spot pairs on one exchange (or all combined) by liquidity, spread, volatility, price and 24h change."""
    d = screener.Filters()
    choices = screener.exchange_choices()
    exchange = request.GET.get("exchange") if request.GET.get("exchange") in dict(choices) else "gate"
    f = screener.Filters(
        quote=(request.GET.get("quote") or d.quote).upper()[:10],
        min_volume=_num(request, "min_volume", d.min_volume),
        max_spread_pct=_num(request, "max_spread_pct", d.max_spread_pct),
        min_range_pct=_num(request, "min_range_pct", d.min_range_pct),
        max_range_pct=_num(request, "max_range_pct", d.max_range_pct),
        min_price=_num(request, "min_price", d.min_price),
        max_price=_num(request, "max_price", d.max_price),
        min_change_pct=_num(request, "min_change_pct", d.min_change_pct),
        max_change_pct=_num(request, "max_change_pct", d.max_change_pct),
        blacklist=tuple((request.GET.get("blacklist") or "").replace(" ", "").split(",")),
        sort=request.GET.get("sort") if request.GET.get("sort") in ("volume", "change", "range", "spread", "venues") else "volume",
        limit=int(_num(request, "limit", d.limit)),
        min_venues=max(1, int(_num(request, "min_venues", d.min_venues))) if exchange == screener.ALL else 1,
    )
    error, rows, total, skipped = "", [], 0, {}
    try:
        if exchange == screener.ALL:
            everything, skipped = screener.combined_rows(f.quote)
        else:
            everything = screener.exchange_rows(exchange, f.quote)
        total = len(everything)
        rows = screener.screen(everything, f)
    except ExchangeError as exc:
        error = f"{dict(choices)[exchange]} unreachable: {exc}"
    on_gate = None if exchange == "gate" else screener.gate_bases(f.quote)
    have = {(c.gate_currency or c.symbol).upper() for c in Coin.objects.filter(quote=f.quote)}
    for r in rows:
        r["added"] = r["base"] in have
        r["on_gate"] = True if exchange == "gate" else (None if on_gate is None else r["base"] in on_gate)
        r["scan_exchange"] = r.get("lead") or exchange
    return render(request, "market/screener.html", {
        "f": f, "rows": rows, "total": total, "error": error, "exchange": exchange, "exchanges": choices,
        "exchange_name": dict(choices)[exchange], "combined": exchange == screener.ALL, "skipped": skipped,
        "gap_warn_pct": screener.GAP_WARN_PCT, "blacklist": ",".join(b for b in f.blacklist if b)})


@login_required
def compare_page(request, pk: int):
    """Gate.io vs Binance (read-only): daily closes, volumes and Binance's taker order flow."""
    coin = get_object_or_404(Coin, pk=pk)
    try:
        days = max(7, min(int(request.GET.get("days") or 30), 365))
    except ValueError:
        days = 30
    error, result = "", None
    try:
        result = compare.compare_daily(coin, days)
    except ExchangeError as exc:
        error = str(exc)
    return render(request, "market/compare.html", {"coin": coin, "r": result, "error": error, "days": days,
                                                   "max_diff": compare.MAX_MEDIAN_DIFF_PCT})


@login_required
@require_GET
def coin_autofill(request):
    """JSON for the Add-coin form: Gate.io identity (primary chain, contract, all chains) + CoinGecko details."""
    symbol = (request.GET.get("symbol") or "").strip().upper()[:32]
    quote = (request.GET.get("quote") or settings.MARKET["DEFAULT_QUOTE"]).strip().upper()[:16]
    if not symbol:
        return JsonResponse({"found": False, "error": "Enter a symbol"}, status=400)
    try:
        info = coininfo.autofill(symbol, quote)
    except ExchangeError as exc:
        return JsonResponse({"found": False, "error": f"Gate.io unreachable: {exc}"}, status=502)
    have = Coin.objects.filter(gate_currency=info.get("symbol", symbol), quote=quote).first()
    info["existing"] = reverse("market:coin_detail", args=[have.pk]) if have else None
    return JsonResponse(info)


@login_required
@require_POST
def coin_enrich(request, pk: int):
    coin = get_object_or_404(Coin, pk=pk)
    try:
        ok = coininfo.enrich(coin)
    except ExchangeError as exc:
        messages.error(request, f"Gate.io unreachable: {exc}")
        return redirect("market:coin_detail", pk=pk)
    if ok and coin.image_url:
        messages.success(request, f"Details refreshed: {len(coin.exchanges)} exchanges, {len(coin.chains)} chains.")
    elif ok:
        messages.warning(request, "Gate.io details refreshed; CoinGecko had no match or is rate-limiting (try later).")
    else:
        messages.error(request, "Gate.io does not list this coin.")
    return redirect("market:coin_detail", pk=pk)


@login_required
def scanner_page(request):
    """Analyse any coin on any supported exchange on demand (nothing stored); add it if it looks promising."""
    from .exchanges.public import EXCHANGES

    symbol = (request.GET.get("symbol") or "").strip().upper()[:20]
    quote = (request.GET.get("quote") or settings.MARKET["DEFAULT_QUOTE"]).strip().upper()[:10]
    exchange = request.GET.get("exchange") if request.GET.get("exchange") in EXCHANGES else "gate"
    try:
        days = int(request.GET.get("days") or scanner.DEFAULT_DAYS)
    except ValueError:
        days = scanner.DEFAULT_DAYS
    days = days if days in scanner.PERIODS else scanner.DEFAULT_DAYS
    width = _zone_width(request)
    ctx = {"symbol": symbol, "quote": quote, "exchange": exchange, "days": days, "width": width,
           "exchanges": [(k, e.name) for k, e in EXCHANGES.items()],
           "periods": [(d, label) for d, (label, _iv) in scanner.PERIODS.items()], "r": None, "where": [], "error": ""}
    if symbol:
        try:
            ctx["r"] = scanner.scan(exchange, symbol, quote, days, width)
        except ExchangeError as exc:
            ctx["error"] = f"{EXCHANGES[exchange].name}: {exc}"
        ctx["where"] = scanner.where_it_trades(symbol, quote)
        gate = next((w for w in ctx["where"] if w["key"] == "gate"), None)
        ctx["on_gate"] = bool(gate and gate.get("listed"))
        ctx["existing"] = Coin.objects.filter(gate_currency=symbol, quote=quote).first()
        r = ctx["r"]
        if r and r.get("bars"):
            ctx["bars_json"] = json.dumps(r["bars"])
            ctx["bands_json"] = json.dumps([{"lower": h["lower"], "upper": h["upper"], "title": f"#{i}"}
                                            for i, h in enumerate(r["hotspots"], 1)])
    return render(request, "market/scanner.html", ctx)
