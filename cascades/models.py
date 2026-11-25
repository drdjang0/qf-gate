from __future__ import annotations

from django.db import models
from django.utils import timezone

from market.models import Coin


class Cascade(models.Model):
    """A buy ladder on one coin: one level rests at a time, each filled level gets its own take-profit sell.

    Dry-run only for now: orders are simulated against Gate.io's real prices (live ticker, the live stream
    and stored 1-minute candles), nothing is sent to the exchange.
    """

    class Kind(models.TextChoices):
        PERCENT = "percent", "% steps"
        MANUAL = "manual", "Manual template"

    class Mode(models.TextChoices):
        DRY_RUN = "dry_run", "Dry-run (simulated fills on real prices)"
        LIVE = "live", "Live (real limit orders on Gate.io)"

    class Status(models.TextChoices):
        PENDING_TRIGGER = "pending_trigger", "Waiting for trigger"
        ACTIVE = "active", "Active"
        HELD = "held", "On hold"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.CASCADE, related_name="cascades")
    name = models.CharField(max_length=100, blank=True)
    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="cascades")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PERCENT)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.DRY_RUN)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    config = models.JSONField(default=dict, blank=True, help_text="The settings it was created from")
    trigger_price = models.FloatField(null=True, blank=True, help_text="Stay inactive until price reaches this")
    fee_pct = models.FloatField(default=0.2, help_text="Fee % per fill, deducted from what is received")
    held_from = models.CharField(max_length=16, blank=True, help_text="Status to go back to on resume")
    watch_from = models.DateTimeField(default=timezone.now, help_text="Trigger checks only use prices from here on")
    last_price = models.FloatField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=0, help_text="Compare-and-set counter: one writer at a time")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Cascade #{self.pk} {self.coin.pair}"

    @property
    def title(self) -> str:
        return self.name or f"{self.coin.pair} cascade #{self.pk}"

    @property
    def is_running(self) -> bool:
        return self.status in (self.Status.PENDING_TRIGGER, self.Status.ACTIVE)

    def totals(self) -> dict:
        """Invested, realised and unrealised P&L (at last_price) over its levels."""
        invested = realized = unrealized = open_qty = 0.0
        for lv in self.levels.all():
            if lv.status != CascadeLevel.Status.FILLED:
                continue
            invested += lv.amount_usdt
            if lv.sell_status == CascadeLevel.SellStatus.FILLED:
                realized += lv.pnl or 0.0
            elif lv.qty:
                open_qty += lv.qty
                if self.last_price:
                    unrealized += lv.qty * self.last_price * (1 - self.fee_pct / 100) - lv.amount_usdt
        return {"invested": invested, "realized": realized, "unrealized": unrealized, "open_qty": open_qty,
                "roi_pct": realized / invested * 100 if invested else 0.0}


class CascadeLevel(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PLACED = "placed", "Buy resting"
        FILLED = "filled", "Bought"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"

    class SellStatus(models.TextChoices):
        NONE = "none", "-"
        PLACED = "placed", "Sell resting"
        FILLED = "filled", "Sold"

    cascade = models.ForeignKey(Cascade, on_delete=models.CASCADE, related_name="levels")
    index = models.PositiveSmallIntegerField()
    target_price = models.FloatField(help_text="Buy limit price")
    amount_usdt = models.FloatField(help_text="Quote amount to spend")
    take_profit_pct = models.FloatField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    skip_reason = models.CharField(max_length=120, blank=True)
    skip_price = models.FloatField(null=True, blank=True)
    placed_at = models.DateTimeField(null=True, blank=True)
    filled_at = models.DateTimeField(null=True, blank=True)
    fill_price = models.FloatField(null=True, blank=True)
    qty = models.FloatField(null=True, blank=True, help_text="Base quantity received (after the fee)")
    buy_fee = models.FloatField(default=0.0, help_text="In the quote currency")
    sell_status = models.CharField(max_length=8, choices=SellStatus.choices, default=SellStatus.NONE)
    sell_target = models.FloatField(null=True, blank=True)
    sell_placed_at = models.DateTimeField(null=True, blank=True)
    sell_filled_at = models.DateTimeField(null=True, blank=True)
    sell_fee = models.FloatField(default=0.0)
    pnl = models.FloatField(null=True, blank=True, help_text="Realised profit of this level's round trip")
    # live cascades: the real orders on Gate.io (client id first, so a crash between sending and saving is recoverable)
    buy_client_id = models.CharField(max_length=32, blank=True)
    buy_order_id = models.CharField(max_length=32, blank=True, db_index=True)
    buy_order_price = models.FloatField(null=True, blank=True, help_text="Price the live buy order was sent at")
    sell_client_id = models.CharField(max_length=32, blank=True)
    sell_order_id = models.CharField(max_length=32, blank=True, db_index=True)
    sell_order_price = models.FloatField(null=True, blank=True)
    order_error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["cascade", "index"]
        constraints = [models.UniqueConstraint(fields=["cascade", "index"], name="uniq_cascade_level")]

    def __str__(self) -> str:
        return f"L{self.index} {self.target_price:g} [{self.status}/{self.sell_status}]"

    def as_dict(self) -> dict:
        return {"index": self.index, "target_price": self.target_price, "amount_usdt": self.amount_usdt,
                "take_profit_pct": self.take_profit_pct, "pk": self.pk}


class CascadeEvent(models.Model):
    """What happened, in order: armed, placed, skipped, filled, sold, held, edited..."""

    cascade = models.ForeignKey(Cascade, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    level = models.PositiveSmallIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=24)
    price = models.FloatField(null=True, blank=True)
    message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-at", "-id"]


class CascadeBacktest(models.Model):
    """A cascade replayed over stored candles. Write-once: the config and results are snapshots."""

    class Status(models.TextChoices):
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.CASCADE, related_name="cascade_backtests")
    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="cascade_backtests")
    name = models.CharField(max_length=100, blank=True)
    kind = models.CharField(max_length=8, choices=Cascade.Kind.choices, default=Cascade.Kind.PERCENT)
    config = models.JSONField(default=dict, blank=True)
    trigger_price = models.FloatField(null=True, blank=True)
    fee_pct = models.FloatField(default=0.2)
    interval = models.CharField(max_length=4)
    start = models.DateTimeField()
    end = models.DateTimeField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DONE)
    message = models.TextField(blank=True)
    bars = models.PositiveIntegerField(default=0)
    levels = models.JSONField(default=list, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    # hoisted for the list view
    roi_pct = models.FloatField(null=True, blank=True)
    realized = models.FloatField(null=True, blank=True)
    fills = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Cascade backtest #{self.pk} {self.coin.pair} {self.interval}"
