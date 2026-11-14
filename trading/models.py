from __future__ import annotations

from datetime import datetime, timezone

from django.db import models
from django.db.models import Q
from django.utils import timezone as dj_tz

from market.models import Coin

INTERVAL_CHOICES = [("1m", "1 minute"), ("5m", "5 minutes"), ("15m", "15 minutes"), ("1h", "1 hour"), ("4h", "4 hours")]


class StrategyConfig(models.Model):
    """A strategy + coin + timeframe + parameters. Backtests and paper sessions run from it."""

    name = models.CharField(max_length=100)
    strategy_key = models.CharField(max_length=64)
    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="strategy_configs")
    interval = models.CharField(max_length=4, choices=INTERVAL_CHOICES, default="15m")
    params = models.JSONField(default=dict, blank=True)
    risk = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # result of the look-ahead / warm-up checks (trading/checks.py) and when they ran
    last_check = models.JSONField(null=True, blank=True)
    last_check_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} · {self.coin.pair} {self.interval}"

    @property
    def strategy_class(self):
        from .strategies.base import get_strategy

        return get_strategy(self.strategy_key)

    def running_paper(self):
        return self.paper_sessions.filter(status=PaperSession.Status.RUNNING).first()


class BacktestRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="backtests")
    # snapshot of the settings used, so later edits to the config don't change past results
    strategy_key = models.CharField(max_length=64)
    interval = models.CharField(max_length=4)
    params = models.JSONField(default=dict)
    risk = models.JSONField(default=dict)
    start = models.DateTimeField()
    end = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    message = models.TextField(blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    equity = models.JSONField(default=list, blank=True, help_text="[[unix_ts, equity], ...] (down-sampled)")
    bars = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # strategy lines for the chart ({name: [[time, value], ...]}) and every signal with what became of it
    plot = models.JSONField(default=dict, blank=True)
    signals = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Backtest #{self.pk} {self.config.name} {self.start:%Y-%m-%d}..{self.end:%Y-%m-%d}"


class Sweep(models.Model):
    """A grid of parameter combinations backtested on the same bars.

    The period is split into an in-sample part (used to rank combinations) and a
    later out-of-sample part (used only to check whether the ranking holds up on
    data the ranking never saw). A combination that only shines in-sample is
    probably curve-fitted.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    RANK_CHOICES = [
        ("return_pct", "Return %"),
        ("profit_factor", "Profit factor"),
        ("sharpe", "Sharpe"),
        ("avg_r", "Average R"),
        ("return_dd", "Return / max drawdown"),
        ("sortino", "Sortino (daily, downside risk)"),
        ("calmar", "Calmar (annual return / max drawdown)"),
        ("combined", "Combined score"),
    ]

    config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="sweeps")
    strategy_key = models.CharField(max_length=64)
    interval = models.CharField(max_length=4)
    base_params = models.JSONField(default=dict)
    base_risk = models.JSONField(default=dict)
    grid = models.JSONField(default=dict, help_text='{"p.target_r": [1, 2, 3], "r.risk_pct": [0.5, 1]}')
    start = models.DateTimeField()
    end = models.DateTimeField()
    oos_pct = models.PositiveSmallIntegerField(default=30)
    rank_by = models.CharField(max_length=16, choices=RANK_CHOICES, default="return_pct")
    min_trades = models.PositiveIntegerField(default=5)
    METHOD_CHOICES = [("grid", "Grid: every combination"), ("smart", "Smart search: sample, then refine around the best")]
    method = models.CharField(max_length=8, choices=METHOD_CHOICES, default="grid")
    budget = models.PositiveIntegerField(default=0, help_text="Smart search: how many combinations to run")
    wf_folds = models.PositiveSmallIntegerField(default=0, help_text="Walk-forward windows (0 = off)")
    walkforward = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    message = models.TextField(blank=True)
    total = models.PositiveIntegerField(default=0)
    done = models.PositiveIntegerField(default=0)
    results = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Sweep #{self.pk} {self.config.name}"

    @property
    def split(self):
        if not self.oos_pct:
            return None
        return self.start + (self.end - self.start) * (1 - self.oos_pct / 100)

    @property
    def progress_pct(self) -> int:
        return int(self.done / self.total * 100) if self.total else 0


class PaperSession(models.Model):
    """Simulated live trading on incoming closed bars, with persistent engine state."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"
        ERROR = "error", "Error"

    config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="paper_sessions")
    strategy_key = models.CharField(max_length=64)
    interval = models.CharField(max_length=4)
    params = models.JSONField(default=dict)
    risk = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(default=dj_tz.now)
    stopped_at = models.DateTimeField(null=True, blank=True)
    state = models.JSONField(default=dict, blank=True)
    last_bar_time = models.BigIntegerField(null=True, blank=True, help_text="Open time (unix) of the last processed bar")
    equity = models.FloatField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["config"], condition=Q(status="running"), name="one_running_paper_per_config"),
        ]

    def __str__(self) -> str:
        return f"Paper #{self.pk} {self.config.name} [{self.status}]"

    @property
    def last_bar_dt(self):
        return datetime.fromtimestamp(self.last_bar_time, tz=timezone.utc) if self.last_bar_time else None


class Trade(models.Model):
    backtest = models.ForeignKey(BacktestRun, null=True, blank=True, on_delete=models.CASCADE, related_name="trades")
    paper = models.ForeignKey(PaperSession, null=True, blank=True, on_delete=models.CASCADE, related_name="trades")
    bot = models.ForeignKey("Bot", null=True, blank=True, on_delete=models.CASCADE, related_name="model_trades")
    side = models.CharField(max_length=5, default="long")
    entry_time = models.DateTimeField()
    entry_price = models.FloatField()
    exit_time = models.DateTimeField()
    exit_price = models.FloatField()
    qty = models.FloatField()
    stop = models.FloatField(null=True, blank=True)
    target = models.FloatField(null=True, blank=True)
    fees = models.FloatField()
    pnl = models.FloatField()
    r_multiple = models.FloatField()
    entry_reason = models.CharField(max_length=255, blank=True)
    entry_tag = models.CharField(max_length=64, blank=True)
    exit_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["entry_time"]
        constraints = [
            models.CheckConstraint(
                condition=Q(backtest__isnull=False) | Q(paper__isnull=False) | Q(bot__isnull=False),
                name="trade_has_owner_v2"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.side} {self.entry_time:%Y-%m-%d %H:%M} pnl={self.pnl:.2f}"

    @classmethod
    def from_engine(cls, t: dict, **owner) -> Trade:
        def ts(x):
            return datetime.fromtimestamp(x, tz=timezone.utc)

        return cls(
            entry_time=ts(t["entry_time"]), exit_time=ts(t["exit_time"]),
            entry_price=t["entry_price"], exit_price=t["exit_price"], qty=t["qty"],
            stop=t.get("stop"), target=t.get("target"), fees=t["fees"], pnl=t["pnl"],
            r_multiple=t["r_multiple"], entry_reason=t.get("entry_reason", "")[:255],
            entry_tag=(t.get("entry_tag") or "")[:64], side=t.get("side", "long"),
            exit_reason=t.get("exit_reason", "")[:64], **owner,
        )


class Bot(models.Model):
    """A strategy trading through a broker: dry-run (simulated fills on Gate's live order book) or live.

    The same Engine as backtests and paper trading makes every decision on closed bars; the bot
    sends the order as soon as the engine decides (not one bar later) and watches live prices for
    the stop and target between bars.
    """

    class Mode(models.TextChoices):
        DRY_RUN = "dry_run", "Dry-run (simulated fills, no orders sent)"
        LIVE = "live", "Live (real orders on Gate.io)"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"
        ERROR = "error", "Error"

    config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="bots")
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.DRY_RUN)
    strategy_key = models.CharField(max_length=64)
    interval = models.CharField(max_length=4)
    params = models.JSONField(default=dict)
    risk = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(default=dj_tz.now)
    stopped_at = models.DateTimeField(null=True, blank=True)
    # {"engine": Engine.state(), "pos": actual position or None, "cash": float, "busy": order in flight}
    state = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=0, help_text="Compare-and-set counter for state updates")
    last_bar_time = models.BigIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["config", "mode"], condition=Q(status="running"),
                                    name="one_running_bot_per_config_mode"),
        ]

    def __str__(self) -> str:
        return f"Bot #{self.pk} {self.config.name} [{self.mode}, {self.status}]"

    @property
    def pos(self):
        return (self.state or {}).get("pos")


class BotOrder(models.Model):
    """Every order a bot sent (or tried to send), with the model price and the actual fill."""

    class Status(models.TextChoices):
        FILLED = "filled", "Filled"
        BLOCKED = "blocked", "Blocked by a risk limit"
        ERROR = "error", "Error"

    bot = models.ForeignKey(Bot, on_delete=models.CASCADE, related_name="orders")
    side = models.CharField(max_length=4)  # buy | sell
    reason = models.CharField(max_length=255, blank=True)
    qty = models.FloatField(help_text="Requested base quantity")
    ref_price = models.FloatField(help_text="Price the decision was made at (last price)")
    status = models.CharField(max_length=8, choices=Status.choices)
    filled_qty = models.FloatField(default=0)
    avg_price = models.FloatField(null=True, blank=True)
    fee = models.FloatField(default=0)
    pnl = models.FloatField(null=True, blank=True, help_text="Sells: realised profit of this round trip")
    exchange_id = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=dj_tz.now)

    class Meta:
        ordering = ["-created_at"]

    @property
    def slippage_bps(self):
        """Fill vs decision price, in basis points; positive = worse for us."""
        if not self.avg_price or not self.ref_price:
            return None
        sign = 1 if self.side == "buy" else -1
        return sign * (self.avg_price / self.ref_price - 1) * 10_000


class TradingControl(models.Model):
    """Account-wide switches and risk limits (a single row, id=1)."""

    halted = models.BooleanField(default=False, help_text="Kill switch: no new entries while on")
    halted_reason = models.CharField(max_length=255, blank=True)
    halted_at = models.DateTimeField(null=True, blank=True)
    live_enabled = models.BooleanField(default=False, help_text="Allow live bots (also needs LIVE_TRADING=true and API keys)")
    max_daily_loss_pct = models.FloatField(default=3.0, help_text="Halt when today's realised loss reaches this % of capital")
    max_exposure = models.FloatField(default=0.0, help_text="Max total open position value in the quote currency (0 = no limit)")
    max_orders_per_hour = models.PositiveIntegerField(default=30)
    min_dry_run_days = models.PositiveIntegerField(default=7, help_text="Dry-run days a config needs before it can go live")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls) -> TradingControl:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PortfolioRun(models.Model):
    """One strategy backtested on several coins with shared cash and a maximum number of open trades."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="portfolios")
    coins = models.ManyToManyField(Coin, related_name="portfolio_runs")
    strategy_key = models.CharField(max_length=64)
    interval = models.CharField(max_length=4)
    params = models.JSONField(default=dict)
    risk = models.JSONField(default=dict)
    start = models.DateTimeField()
    end = models.DateTimeField()
    max_open = models.PositiveSmallIntegerField(default=3)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    message = models.TextField(blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    equity = models.JSONField(default=list, blank=True)
    trades = models.JSONField(default=list, blank=True)
    per_coin = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class LiveSignal(models.Model):
    """A signal from a running paper session / bot (source "local") or received from another instance."""

    source = models.CharField(max_length=64, default="local", help_text="local, or the name of the remote instance")
    config_name = models.CharField(max_length=100, blank=True)
    pair = models.CharField(max_length=40)
    interval = models.CharField(max_length=4, blank=True)
    action = models.CharField(max_length=8)
    stop = models.FloatField(null=True, blank=True)
    target = models.FloatField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    bar_time = models.BigIntegerField(help_text="Open time (unix) of the bar that produced the signal")
    remote_id = models.BigIntegerField(null=True, blank=True)
    acted = models.BooleanField(default=False, help_text="Remote signals: already handed to follow bots")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["source", "config_name", "pair", "action", "bar_time"],
                                               name="uniq_live_signal")]
