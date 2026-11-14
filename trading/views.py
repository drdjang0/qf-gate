from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone as dj_tz
from django.views.decorators.http import require_POST

from market.models import Job
from market.services import jobs

from django.http import HttpResponse

from . import checks, paper, report
from . import sweep as sweep_mod
from .backtest import run_backtest
from .forms import BacktestForm, StrategyConfigForm, SweepForm
from .models import BacktestRun, PaperSession, StrategyConfig, Sweep
from .strategies.base import REGISTRY, RISK_PARAMS, strategy_choices


def _trades_json(qs) -> list[dict]:
    return [
        {"entry_time": int(t.entry_time.timestamp()), "exit_time": int(t.exit_time.timestamp()),
         "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": t.pnl, "r": t.r_multiple,
         "exit_reason": t.exit_reason}
        for t in qs
    ]


@login_required
def strategy_list(request):
    configs = StrategyConfig.objects.select_related("coin").prefetch_related("backtests", "paper_sessions")
    rows = []
    for c in configs:
        last = next((b for b in c.backtests.all() if b.status == BacktestRun.Status.DONE), None)
        running = next((p for p in c.paper_sessions.all() if p.status == PaperSession.Status.RUNNING), None)
        rows.append({"config": c, "last_backtest": last, "paper": running})
    return render(request, "trading/strategy_list.html", {"rows": rows})


@login_required
def strategy_create(request):
    strategy_choices()  # make sure built-in strategies are registered
    key = request.GET.get("strategy") or request.POST.get("strategy")
    if key not in REGISTRY:
        return render(request, "trading/strategy_pick.html", {"strategies": REGISTRY.values()})
    form = StrategyConfigForm(request.POST or None, strategy_key=key)
    if request.method == "POST" and form.is_valid():
        cfg = form.save()
        messages.success(request, f"Created {cfg.name}. Run a backtest to see how it would have done.")
        return redirect("trading:strategy_detail", pk=cfg.pk)
    return render(request, "trading/strategy_form.html", {"form": form, "strategy": REGISTRY[key]})


@login_required
def strategy_edit(request, pk: int):
    cfg = get_object_or_404(StrategyConfig, pk=pk)
    form = StrategyConfigForm(request.POST or None, instance=cfg, strategy_key=cfg.strategy_key)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved. Past backtests keep the settings they ran with; a running paper "
                                  "session keeps its settings until you restart it.")
        return redirect("trading:strategy_detail", pk=cfg.pk)
    return render(request, "trading/strategy_form.html", {"form": form, "strategy": cfg.strategy_class, "object": cfg})


@login_required
@require_POST
def strategy_delete(request, pk: int):
    cfg = get_object_or_404(StrategyConfig, pk=pk)
    name = cfg.name
    cfg.delete()
    messages.success(request, f"Deleted {name} with its backtests and paper sessions.")
    return redirect("trading:strategy_list")


@login_required
def strategy_detail(request, pk: int):
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    return render(request, "trading/strategy_detail.html", {
        "config": cfg,
        "strategy": cfg.strategy_class,
        "params": cfg.strategy_class(cfg.params).p,
        "risk": {k: cfg.risk.get(k, p.default) for k, p in RISK_PARAMS.items()},
        "backtests": cfg.backtests.all()[:20],
        "sweeps": cfg.sweeps.all()[:10],
        "sessions": cfg.paper_sessions.all()[:10],
        "running": cfg.running_paper(),
        "bt_form": BacktestForm(),
        "bots": cfg.bots.all()[:10],
        "running_bots": {b.mode for b in cfg.bots.filter(status="running")},
        "promotion": bots_mod.promotion_status(cfg),
        "all_coins": Coin.objects.filter(verified_at__isnull=False, is_active=True).order_by("symbol"),
        "portfolios": cfg.portfolios.all()[:10],
    })


@login_required
@require_POST
def strategy_check(request, pk: int):
    """Look-ahead and warm-up checks on the last 30 days of stored data (runs in the request: a few seconds)."""
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    try:
        result = checks.run_checks(cfg)
    except Exception as exc:  # noqa: BLE001 - shown to the user
        messages.error(request, f"Check failed: {exc}")
        return redirect("trading:strategy_detail", pk=pk)
    cfg.last_check, cfg.last_check_at = result, dj_tz.now()
    cfg.save(update_fields=["last_check", "last_check_at"])
    ok = result["lookahead"]["ok"] and result["warmup"]["ok"] is not False
    (messages.success if ok else messages.warning)(
        request, "Checks passed: no look-ahead found and the warm-up is long enough." if ok
        else "Checks found a problem - see the Strategy checks section.")
    return redirect("trading:strategy_detail", pk=pk)


@login_required
@require_POST
def backtest_create(request, pk: int):
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    form = BacktestForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errs in form.errors.values() for e in errs))
        return redirect("trading:strategy_detail", pk=pk)
    start = datetime.combine(form.cleaned_data["start"], time.min, tzinfo=timezone.utc)
    end = min(datetime.combine(form.cleaned_data["end"] + timedelta(days=1), time.min, tzinfo=timezone.utc), dj_tz.now())
    run = BacktestRun.objects.create(
        config=cfg, strategy_key=cfg.strategy_key, interval=cfg.interval, params=cfg.params, risk=cfg.risk,
        start=start, end=end,
    )
    worker = jobs.service_status()["worker"]
    if worker and worker.is_alive:
        jobs.enqueue(Job.Kind.BACKTEST, cfg.coin, {"run_id": run.pk}, dedupe=False)
        messages.info(request, "Backtest queued; this page updates when it finishes.")
    else:
        try:
            run_backtest(run)  # no worker running: a backtest takes seconds, so run it right away
        except Exception as exc:  # noqa: BLE001 - shown on the run page
            messages.error(request, f"Backtest failed: {exc}")
    return redirect("trading:backtest_detail", pk=run.pk)


@login_required
def backtest_detail(request, pk: int):
    run = get_object_or_404(BacktestRun.objects.select_related("config__coin"), pk=pk)
    trades = list(run.trades.all())
    starting = run.risk.get("starting_cash", RISK_PARAMS["starting_cash"].default)
    return render(request, "trading/backtest_detail.html", {
        "run": run, "trades": trades, "trades_json": _trades_json(trades), "m": run.metrics,
        "breakdowns": report.breakdowns(trades, starting), "groups": report.GROUPS,
        "export_url": reverse("trading:backtest_export", args=[run.pk]),
        "plot_json": run.plot or {}, "signals_json": [s for s in run.signals if s["status"] != "queued"][:2000],
        "signal_counts": _signal_counts(run.signals),
        "chart": {"interval": run.interval, "start": int(run.start.timestamp()), "end": int(run.end.timestamp()),
                  "coin": run.config.coin_id, "live": False},
    })


@login_required
def sweep_create(request, pk: int):
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    form = SweepForm(request.POST or None, config=cfg)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        sw = Sweep.objects.create(
            config=cfg, strategy_key=cfg.strategy_key, interval=cfg.interval,
            base_params=cfg.params, base_risk=cfg.risk, grid=d["grid"],
            start=datetime.combine(d["start"], time.min, tzinfo=timezone.utc),
            end=min(datetime.combine(d["end"] + timedelta(days=1), time.min, tzinfo=timezone.utc), dj_tz.now()),
            oos_pct=d["oos_pct"] or 0, rank_by=d["rank_by"], min_trades=d["min_trades"] or 0, total=d["runs"],
            method=d["method"], budget=d.get("budget") or 0, wf_folds=d.get("wf_folds") or 0,
        )
        worker = jobs.service_status()["worker"]
        if worker and worker.is_alive:
            jobs.enqueue(Job.Kind.SWEEP, cfg.coin, {"sweep_id": sw.pk}, dedupe=False)
            messages.info(request, f"Sweep queued for the worker ({d['runs']} backtests).")
        else:
            sweep_mod.run_in_thread(sw.pk)
            messages.info(request, f"Sweep started ({d['runs']} backtests; no worker running, so it runs "
                                   "inside the web server). This page updates as it progresses.")
        return redirect("trading:sweep_detail", pk=sw.pk)
    return render(request, "trading/sweep_form.html", {"form": form, "config": cfg, "max_combos": sweep_mod.MAX_COMBOS})


def _fmt_combo(combo: dict) -> list[tuple[str, object]]:
    return [(k.split(".", 1)[1], v) for k, v in combo.items()]


@login_required
def sweep_detail(request, pk: int):
    sw = get_object_or_404(Sweep.objects.select_related("config__coin"), pk=pk)
    varied = sweep_mod.varied_keys(sw.grid)
    rows = []
    for i, r in enumerate(sw.results[:100]):
        # "ins" alias: `is` is an operator inside Django {% if %} tags
        rows.append({**r, "ins": r["is"], "index": i, "varied": [(k.split(".", 1)[1], r["combo"][k]) for k in varied]})
    top = [r for r in sw.results if r["enough_trades"]][:10]
    oos_top = [r["oos"]["return_pct"] for r in top if r["oos"]]
    summary = None
    if top and sw.oos_pct:
        oos_pos = sum(1 for v in oos_top if v > 0)
        summary = {
            "best_is": top[0]["is"]["return_pct"],
            "best_oos": top[0]["oos"]["return_pct"] if top[0]["oos"] else None,
            "top_n": len(top),
            "oos_positive": oos_pos,
            "oos_median": sorted(oos_top)[len(oos_top) // 2] if oos_top else None,
        }
    return render(request, "trading/sweep_detail.html", {
        "sw": sw, "rows": rows, "varied": [k.split(".", 1)[1] for k in varied],
        "grid": [(k.split(".", 1)[1], v) for k, v in sw.grid.items()],
        "heatmap": sweep_mod.heatmap(sw) if sw.status == Sweep.Status.DONE else None,
        "summary": summary,
        "wf": sw.walkforward,
        "wf_rows": [{**f, "varied": [(k.split(".", 1)[1], f["combo"].get(k)) for k in varied]}
                    for f in (sw.walkforward or {}).get("folds", [])],
    })


@login_required
@require_POST
def sweep_apply(request, pk: int, index: int):
    """Save a sweep row as a NEW strategy (the original stays untouched)."""
    sw = get_object_or_404(Sweep.objects.select_related("config"), pk=pk)
    try:
        row = sw.results[index]
    except IndexError:
        messages.error(request, "That result no longer exists.")
        return redirect("trading:sweep_detail", pk=pk)
    params, risk = sweep_mod.split_combo(row["combo"], sw.base_params, sw.base_risk)
    cfg = StrategyConfig.objects.create(
        name=f"{sw.config.name} · sweep #{sw.pk} rank {index + 1}"[:100],
        strategy_key=sw.strategy_key, coin=sw.config.coin, interval=sw.interval, params=params, risk=risk,
        notes=f"Created from sweep #{sw.pk}, rank {index + 1}: " + ", ".join(f"{k}={v}" for k, v in _fmt_combo(row["combo"])),
    )
    messages.success(request, "Saved as a new strategy. Backtest it on a different period, then paper-trade it, "
                              "before trusting it.")
    return redirect("trading:strategy_detail", pk=cfg.pk)


@login_required
@require_POST
def paper_start(request, pk: int):
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    if not cfg.coin.verified_at or not cfg.coin.is_active:
        messages.error(request, "The coin must be active and verified.")
        return redirect("trading:strategy_detail", pk=pk)
    try:
        s = paper.start_session(cfg)
    except IntegrityError:
        messages.info(request, "A paper session is already running for this strategy.")
        return redirect("trading:strategy_detail", pk=pk)
    paper.step_session(s.pk)  # warm up immediately so the page has state
    messages.success(request, "Paper trading started. It trades on bars that close from now on.")
    status = jobs.service_status()
    if not (status["stream"] and status["stream"].is_alive) and not (status["worker"] and status["worker"].is_alive):
        messages.warning(request, "Neither stream_candles nor run_worker is running, so no new bars will arrive.")
    return redirect("trading:paper_detail", pk=s.pk)


@login_required
@require_POST
def paper_stop(request, pk: int):
    s = get_object_or_404(PaperSession, pk=pk)
    paper.stop_session(s)
    messages.success(request, "Paper session stopped. An open simulated position stays as it was.")
    return redirect("trading:paper_detail", pk=pk)


@login_required
def paper_detail(request, pk: int):
    s = get_object_or_404(PaperSession.objects.select_related("config__coin"), pk=pk)
    trades = list(s.trades.all())
    st = s.state or {}
    starting = s.risk.get("starting_cash", RISK_PARAMS["starting_cash"].default)
    equity = s.equity if s.equity is not None else starting
    return render(request, "trading/paper_detail.html", {
        "s": s, "trades": list(reversed(trades)), "trades_json": _trades_json(trades),
        "pos": st.get("pos"), "pending": st.get("pending"), "cash": st.get("cash", starting),
        "equity": equity, "starting": starting, "ret_pct": (equity / starting - 1) * 100,
        "realized": sum(t.pnl for t in trades),
        "breakdowns": report.breakdowns(trades, starting), "groups": report.GROUPS,
        "export_url": reverse("trading:paper_export", args=[s.pk]),
        "blocked": st.get("blocked") or {},
        "chart": {"interval": s.interval, "start": int((s.started_at - timedelta(days=1)).timestamp()), "end": None,
                  "coin": s.config.coin_id, "live": s.status == PaperSession.Status.RUNNING},
    })


def _signal_counts(signals: list[dict]) -> dict:
    out: dict[str, int] = {}
    for s in signals:
        out[s["status"]] = out.get(s["status"], 0) + 1
    return out


@login_required
def backtest_signals_csv(request, pk: int):
    import csv
    import io

    run = get_object_or_404(BacktestRun, pk=pk)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "action", "status", "reason", "tag", "stop", "target"])
    for s in run.signals:
        w.writerow([datetime.fromtimestamp(s["time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"), s["action"],
                    s["status"], s["reason"], s.get("tag", ""), s["stop"], s["target"]])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="backtest-{run.pk}-signals.csv"'
    return resp


def _xlsx(data: bytes, name: str) -> HttpResponse:
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@login_required
def backtest_export(request, pk: int):
    run = get_object_or_404(BacktestRun.objects.select_related("config__coin"), pk=pk)
    starting = run.risk.get("starting_cash", RISK_PARAMS["starting_cash"].default)
    title = f"Backtest #{run.pk} {run.config.name} {run.interval} {run.start:%Y-%m-%d} to {run.end:%Y-%m-%d}"
    return _xlsx(report.workbook(title, list(run.trades.all()), starting, run.metrics), f"backtest-{run.pk}.xlsx")


@login_required
def paper_export(request, pk: int):
    s = get_object_or_404(PaperSession.objects.select_related("config__coin"), pk=pk)
    starting = s.risk.get("starting_cash", RISK_PARAMS["starting_cash"].default)
    title = f"Paper session #{s.pk} {s.config.name} {s.interval} since {s.started_at:%Y-%m-%d}"
    return _xlsx(report.workbook(title, list(s.trades.all()), starting), f"paper-{s.pk}.xlsx")


# -- Phase 4: bots, control, kill switch ------------------------------------------------------------

from . import bots as bots_mod  # noqa: E402
from . import notify  # noqa: E402
from .brokers import live_allowed  # noqa: E402
from .models import Bot, BotOrder, TradingControl  # noqa: E402


@login_required
@require_POST
def bot_start(request, pk: int):
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    mode = request.POST.get("mode", Bot.Mode.DRY_RUN)
    if mode not in Bot.Mode.values:
        mode = Bot.Mode.DRY_RUN
    try:
        bot = bots_mod.start_bot(cfg, mode)
    except IntegrityError:
        messages.warning(request, f"A {mode.replace('_', '-')} bot is already running for this strategy.")
        return redirect("trading:strategy_detail", pk=pk)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("trading:strategy_detail", pk=pk)
    messages.success(request, f"Bot #{bot.pk} started ({bot.get_mode_display()}). It trades from the next closed bar.")
    return redirect("trading:bot_detail", pk=bot.pk)


@login_required
def bot_detail(request, pk: int):
    bot = get_object_or_404(Bot.objects.select_related("config__coin"), pk=pk)
    st = bot.state or {}
    risk = {**{k: p.default for k, p in RISK_PARAMS.items()}, **bot.risk}
    starting = risk["starting_cash"]
    orders = list(bot.orders.all()[:200])
    filled = [o for o in orders if o.status == BotOrder.Status.FILLED]
    sells = [o for o in filled if o.side == "sell"]
    slips = [o.slippage_bps for o in filled if o.slippage_bps is not None]
    pos = st.get("pos")
    last = None
    if pos:
        try:
            last = float((bots_mod.get_client().get_ticker(bot.config.coin.pair) or {}).get("last") or 0) or None
        except Exception:  # noqa: BLE001 - the page still renders without a live price
            last = None
    cash = st.get("cash", starting)
    equity = cash + (pos["qty"] * (last or pos["avg"]) if pos else 0)
    model = list(bot.model_trades.all())
    eng = st.get("engine") or {}
    return render(request, "trading/bot_detail.html", {
        "bot": bot, "pos": pos, "last": last, "cash": cash, "equity": equity, "starting": starting,
        "ret_pct": (equity / starting - 1) * 100, "orders": orders,
        "realised": sum(o.pnl or 0 for o in sells), "wins": sum(1 for o in sells if (o.pnl or 0) > 0),
        "round_trips": len(sells), "avg_slip": sum(slips) / len(slips) if slips else None,
        "fees": sum(o.fee for o in filled),
        "model_pnl": sum(t.pnl for t in model), "model_trades": len(model),
        "unreal": (last - pos["avg"]) * pos["qty"] if pos and last else None,
        "pending": eng.get("pending"), "control": TradingControl.get(),
    })


@login_required
@require_POST
def bot_stop(request, pk: int):
    bot = get_object_or_404(Bot.objects.select_related("config"), pk=pk)
    bots_mod.stop_bot(bot, note="stopped from the web page")
    still = Bot.objects.get(pk=pk).pos
    messages.info(request, f"Bot #{bot.pk} stopped." + (" Its position is still open." if still else ""))
    return redirect("trading:bot_detail", pk=pk)


@login_required
@require_POST
def bot_force_exit(request, pk: int):
    bot = get_object_or_404(Bot, pk=pk)
    o = bots_mod.force_exit(bot, by="web")
    if o and o.status == BotOrder.Status.FILLED:
        messages.success(request, f"Sold {o.filled_qty:.6g} at {o.avg_price:,.6g} (P&L {o.pnl:+,.2f}).")
    else:
        messages.warning(request, "No position to exit, or the order failed (see the order log).")
    return redirect("trading:bot_detail", pk=pk)


@login_required
def control(request):
    c = TradingControl.get()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "halt":
            bots_mod.halt(request.POST.get("reason") or "manual halt", by="web")
            messages.warning(request, "Trading halted: no new entries. Open positions are still managed.")
        elif action == "resume":
            bots_mod.resume(by="web")
            messages.success(request, "Entries allowed again.")
        elif action == "kill":
            if request.POST.get("confirm") != "KILL":
                messages.error(request, "Type KILL to confirm the kill switch.")
            else:
                n = bots_mod.kill(by="web")
                messages.warning(request, f"Kill switch: {n} position(s) sold, all bots stopped, entries halted.")
        elif action == "limits":
            try:
                c.max_daily_loss_pct = max(0.0, float(request.POST.get("max_daily_loss_pct") or 0))
                c.max_exposure = max(0.0, float(request.POST.get("max_exposure") or 0))
                c.max_orders_per_hour = max(1, int(request.POST.get("max_orders_per_hour") or 1))
                c.min_dry_run_days = max(0, int(request.POST.get("min_dry_run_days") or 0))
                c.save()
                messages.success(request, "Risk limits saved.")
            except ValueError:
                messages.error(request, "Limits must be numbers.")
        elif action == "live":
            want = request.POST.get("enable") == "1"
            if want and request.POST.get("confirm") != "LIVE":
                messages.error(request, "Type LIVE to confirm switching live trading on.")
            else:
                c.live_enabled = want
                c.save(update_fields=["live_enabled", "updated_at"])
                messages.warning(request, "Live trading switch ON." if want else "Live trading switch OFF.")
        elif action == "test":
            if any(notify.configured().values()):
                notify.notify("test", "Test alert from the Control page.", background=False)
                messages.success(request, "Test alert sent.")
            else:
                messages.warning(request, "No Telegram or webhook configured (see .env.example).")
        return redirect("trading:control")
    ok_live, missing = live_allowed()
    return render(request, "trading/control.html", {
        "c": c, "bots": Bot.objects.select_related("config__coin")[:50],
        "orders": BotOrder.objects.select_related("bot__config__coin")[:30],
        "live_ok": ok_live, "live_missing": missing, "notify": notify.configured(),
        "today": {m: bots_mod.realised_today(m) for m in Bot.Mode.values},
        "exposure": {m: bots_mod.exposure(m) for m in Bot.Mode.values},
        "running": Bot.objects.filter(status=Bot.Status.RUNNING).count(),
        "signals": LiveSignal.objects.all()[:20],
        "signal_api": bool(settings.TRADING.get("SIGNAL_API_TOKEN")),
    })


# -- Phase 5: portfolio backtests -------------------------------------------------------------------

from market.models import Coin  # noqa: E402

from . import portfolio as portfolio_mod  # noqa: E402
from .models import PortfolioRun  # noqa: E402


@login_required
@require_POST
def portfolio_create(request, pk: int):
    cfg = get_object_or_404(StrategyConfig.objects.select_related("coin"), pk=pk)
    ids = [int(x) for x in request.POST.getlist("coins") if x.isdigit()]
    coins = list(Coin.objects.filter(pk__in=ids, verified_at__isnull=False))
    form = BacktestForm(request.POST)
    if len(coins) < 1 or not form.is_valid():
        messages.error(request, "Pick at least one verified coin and a valid period.")
        return redirect("trading:strategy_detail", pk=pk)
    try:
        max_open = max(1, min(int(request.POST.get("max_open") or 3), 50))
    except ValueError:
        max_open = 3
    start = datetime.combine(form.cleaned_data["start"], time.min, tzinfo=timezone.utc)
    end = min(datetime.combine(form.cleaned_data["end"] + timedelta(days=1), time.min, tzinfo=timezone.utc), dj_tz.now())
    run = PortfolioRun.objects.create(config=cfg, strategy_key=cfg.strategy_key, interval=cfg.interval, params=cfg.params,
                                      risk=cfg.risk, start=start, end=end, max_open=max_open)
    run.coins.set(coins)
    worker = jobs.service_status()["worker"]
    if worker and worker.is_alive:
        jobs.enqueue(Job.Kind.PORTFOLIO, cfg.coin, {"portfolio_id": run.pk}, dedupe=False)
        messages.info(request, "Portfolio backtest queued; this page updates when it finishes.")
    else:
        try:
            portfolio_mod.run_portfolio(run)
        except Exception as exc:  # noqa: BLE001 - shown on the run page
            messages.error(request, f"Portfolio backtest failed: {exc}")
    return redirect("trading:portfolio_detail", pk=run.pk)


@login_required
def portfolio_detail(request, pk: int):
    run = get_object_or_404(PortfolioRun.objects.select_related("config"), pk=pk)
    trades = sorted(run.trades, key=lambda t: t["entry_time"], reverse=True)
    for t in trades:
        t["entry_dt"] = datetime.fromtimestamp(t["entry_time"], tz=timezone.utc)
        t["exit_dt"] = datetime.fromtimestamp(t["exit_time"], tz=timezone.utc)
    return render(request, "trading/portfolio_detail.html", {
        "run": run, "m": run.metrics, "coins": run.coins.all(), "trades": trades[:500],
        "per_coin": sorted(run.per_coin.items(), key=lambda kv: kv[1]["pnl"]),
    })


# -- Phase 6.3: signal sharing API ------------------------------------------------------------------

import json as _json  # noqa: E402

from django.http import JsonResponse  # noqa: E402
from django.views.decorators.csrf import csrf_exempt  # noqa: E402

from . import signals_share  # noqa: E402
from .models import LiveSignal  # noqa: E402


@csrf_exempt
def api_signals(request):
    """GET: this instance's local signals (?since=<id>). Bearer SIGNAL_API_TOKEN required."""
    if request.method != "GET":
        return JsonResponse({"error": "GET only"}, status=405)
    if not signals_share.token_ok(request.headers.get("Authorization")):
        return JsonResponse({"error": "unauthorised"}, status=401)
    try:
        since = int(request.GET.get("since") or 0)
    except ValueError:
        since = 0
    qs = LiveSignal.objects.filter(source="local", pk__gt=since).order_by("id")[:500]
    return JsonResponse({"signals": [signals_share.serialize(x) for x in qs]})


@csrf_exempt
def api_ingest(request):
    """POST {"source": "<name>", "signals": [...]} from another instance. Bearer SIGNAL_API_TOKEN required."""
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    if not signals_share.token_ok(request.headers.get("Authorization")):
        return JsonResponse({"error": "unauthorised"}, status=401)
    try:
        body = _json.loads(request.body or b"{}")
        source = str(body.get("source") or "remote")[:64]
        items = body.get("signals") or []
        if source == "local" or not isinstance(items, list):
            raise ValueError("bad payload")
    except (ValueError, AttributeError):
        return JsonResponse({"error": "expected {source, signals: [...]}"}, status=400)
    new = signals_share.ingest(source, items)
    sent = signals_share.dispatch()
    return JsonResponse({"stored": len(new), "orders": sent})
