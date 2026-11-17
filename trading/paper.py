"""Paper trading: run a strategy on live closed bars with simulated fills.

Each step loads the bars that closed since the last step and feeds them to the
same Engine the backtester uses, starting from the persisted engine state. A
step runs when the live stream closes a minute (low latency) and every minute
from the worker (safety net). A compare-and-set on `last_bar_time` makes
concurrent steps safe: only one of them can commit a given bar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_tz

from market.models import Coin
from market.services.chart import CHART_INTERVALS

from .backtest import warmup
from market.services.funding import funding_map

from . import signals_share
from .data import load_bars
from .engine import Engine
from .models import PaperSession, StrategyConfig, Trade
from .strategies.base import get_strategy

log = logging.getLogger(__name__)


def start_session(config: StrategyConfig) -> PaperSession:
    get_strategy(config.strategy_key)(config.params)  # validate before starting
    with transaction.atomic():  # savepoint: a duplicate start raises IntegrityError cleanly
        return PaperSession.objects.create(
            config=config, strategy_key=config.strategy_key, interval=config.interval,
            params=config.params, risk=config.risk,
        )


def stop_session(session: PaperSession) -> None:
    PaperSession.objects.filter(pk=session.pk, status=PaperSession.Status.RUNNING).update(
        status=PaperSession.Status.STOPPED, stopped_at=dj_tz.now()
    )


def step_session(session_id: int, now: datetime | None = None) -> int:
    """Process newly closed bars for one session. Returns the number of trades closed."""
    s = PaperSession.objects.select_related("config__coin").get(pk=session_id)
    if s.status != PaperSession.Status.RUNNING:
        return 0
    strategy = get_strategy(s.strategy_key)(s.params)  # fail fast on a broken config
    now = now or datetime.now(timezone.utc)
    step = CHART_INTERVALS[s.interval]
    if s.last_bar_time is None:
        from_dt = s.started_at - warmup(s.strategy_key, s.params)
    else:
        from_dt = datetime.fromtimestamp(s.last_bar_time + step, tz=timezone.utc)
    bars = load_bars(s.config.coin, s.interval, from_dt, now + timedelta(minutes=1))
    if not bars:
        return 0

    prev_pending = (s.state or {}).get("pending")
    engine = Engine(strategy, s.risk, settings.MARKET["DAY_TIMEZONE"], state=s.state or None,
                    funding=funding_map(s.config.coin, from_dt, now + timedelta(minutes=1)))
    from intel.gate import make_gate  # 7.7 AI filter (no-op unless switched on and earned)

    engine.entry_gate = make_gate(s.config.coin, s.risk, "paper")
    start_ts = int(s.started_at.timestamp())
    closed: list[dict] = []
    for bar in bars:
        closed += engine.process(bar, trading=bar.time >= start_ts)

    with transaction.atomic():
        updated = PaperSession.objects.filter(
            pk=s.pk, status=PaperSession.Status.RUNNING, last_bar_time=s.last_bar_time
        ).update(state=engine.state(), last_bar_time=engine.last_time, equity=engine.equity(),
                 last_error="", updated_at=dj_tz.now())
        if not updated:
            return 0  # another step committed these bars first (or the session was stopped)
        Trade.objects.bulk_create([Trade.from_engine(t, paper_id=s.pk) for t in closed])
    if engine.pending and engine.pending != prev_pending:
        signals_share.record(s.config.name, s.config.coin.pair, s.interval, engine.pending, engine.last_time)
    return len(closed)


def step_all(coin: Coin | None = None) -> int:
    qs = PaperSession.objects.filter(status=PaperSession.Status.RUNNING)
    if coin is not None:
        qs = qs.filter(config__coin=coin)
    total = 0
    for sid in qs.values_list("id", flat=True):
        try:
            total += step_session(sid)
        except Exception as exc:  # noqa: BLE001 - one broken strategy must not stop the others
            log.exception("Paper session %s failed", sid)
            PaperSession.objects.filter(pk=sid).update(status=PaperSession.Status.ERROR, last_error=str(exc)[:4000])
    return total
