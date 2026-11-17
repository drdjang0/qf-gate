from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from market.exchanges.public import EXCHANGES
from market.models import Coin
from market.services.jobs import service_status

from . import briefs, llm, macro, news, scorecard
from .models import Brief, NewsItem


@login_required
def index(request):
    """Intel: macro panel, news feed, AI status and spend, briefs and their scorecard."""
    q = (request.GET.get("q") or "").strip().upper()[:20]
    if q:
        items = news.items_for(q, hours=24 * 30, limit=60)
    else:
        items = list(NewsItem.objects.all()[:60])
    return render(request, "intel/index.html", {
        "q": q, "items": items, "mood": news.mood(items), "sources": news.source_status(),
        "macro": macro.panel(), "ai": llm.status(), "briefs": Brief.objects.select_related("coin")[:30],
        "score": scorecard.scorecard(), "earned": scorecard.filter_earned(),
        "coins": Coin.objects.filter(is_active=True).order_by("symbol"), "exchanges": [(k, e.name) for k, e in EXCHANGES.items()],
    })


@login_required
@require_POST
def news_refresh(request):
    st = news.fetch_all()
    ok = [k for k, v in st.items() if v["ok"]]
    new = sum(v["new"] for v in st.values())
    messages.success(request, f"News refreshed: {new} new items from {len(ok)} of {len(st)} sources.")
    for k, v in st.items():
        if not v["ok"]:
            messages.warning(request, f"{k}: {v['error']}")
    return redirect(request.POST.get("next") or "intel:index")


@login_required
@require_POST
def macro_refresh(request):
    p = macro.refresh()
    for k, e in p["errors"].items():
        messages.warning(request, f"{k}: {e}")
    return redirect("intel:index")


@login_required
def card(request):
    """Fragment for the Market / Scanner / coin pages: news for the coin, its latest briefs, the Brief button."""
    symbol = (request.GET.get("symbol") or "").strip().upper()[:20]
    quote = (request.GET.get("quote") or "USDT").strip().upper()[:10]
    exchange = request.GET.get("exchange") if request.GET.get("exchange") in EXCHANGES else "gate"
    coin = Coin.objects.filter(pk=request.GET.get("coin")).first() if (request.GET.get("coin") or "").isdigit() else None
    items = news.items_for(symbol, getattr(coin, "name", "") or "", hours=72, limit=8) if symbol else []
    return render(request, "intel/_card.html", {
        "symbol": symbol, "quote": quote, "exchange": exchange, "coin": coin, "items": items, "mood": news.mood(items),
        "ai": llm.enabled(), "briefs": Brief.objects.filter(symbol=symbol, quote=quote)[:3] if symbol else [],
    })


@login_required
@require_POST
def brief_create(request):
    symbol = (request.POST.get("symbol") or "").strip().upper()[:20]
    if not symbol:
        messages.error(request, "Pick a coin first.")
        return redirect("intel:index")
    coin = Coin.objects.filter(pk=request.POST.get("coin")).first() if (request.POST.get("coin") or "").isdigit() else None
    try:
        b = briefs.create(symbol, request.POST.get("quote") or "USDT", request.POST.get("exchange") or "gate", coin)
    except llm.LLMError as exc:
        messages.error(request, str(exc))
        return redirect("intel:index")
    worker = service_status().get("worker")
    if not (worker and worker.is_alive):
        briefs.start(b.pk)  # no worker: run it inside the web server
    return redirect("intel:brief_detail", pk=b.pk)


@login_required
def brief_detail(request, pk: int):
    b = get_object_or_404(Brief.objects.select_related("coin"), pk=pk)
    rep = b.reports or {}
    return render(request, "intel/brief_detail.html", {
        "b": b, "analysts": [(k, rep.get(k)) for k in briefs.ANALYSTS if rep.get(k)],
        "debate": rep.get("debate") or [], "manager": rep.get("research_manager"), "risk": rep.get("risk") or {},
        "final_raw": rep.get("portfolio_manager"), "facts": (b.inputs or {}).get("fact_sheet", ""),
        "score": scorecard.SCORES.get(b.rating),
    })
