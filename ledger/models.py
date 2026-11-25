"""The transaction ledger: one permanent record of every buy, sell, deposit and withdrawal.

Every route writes here (bots, paper trading, cascades, manual entries, the Gate.io account sync), each
row tagged with its account, route and a unique `source_key`, so re-running a sync or a backfill can
never record the same fill twice. Real and simulated money live in separate accounts and are never
added together.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from market.models import Coin

from .fields import ExactDecimalField


class Account(models.Model):
    class Kind(models.TextChoices):
        REAL = "real", "Real money"
        SIMULATED = "simulated", "Simulated"

    class Sync(models.TextChoices):
        NONE = "none", "None (QF-gate records it, or you enter it)"
        GATE = "gate", "Gate.io API (read-only key)"

    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.CASCADE, related_name="ledger_accounts")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.REAL)
    exchange = models.CharField(max_length=30, default="Gate.io", blank=True)
    sync = models.CharField(max_length=8, choices=Sync.choices, default=Sync.NONE)
    history_start = models.DateField(null=True, blank=True,
                                     help_text="Gate sync: how far back the first import walks")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "id"]
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="ledger_account_name_per_owner")]

    def __str__(self) -> str:
        return self.name

    @property
    def is_real(self) -> bool:
        return self.kind == self.Kind.REAL


class Transaction(models.Model):
    class Kind(models.TextChoices):
        TRADE = "trade", "Trade"
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        ADJUSTMENT = "adjustment", "Adjustment"

    class Market(models.TextChoices):
        SPOT = "spot", "Spot"
        MARGIN = "margin", "Margin / short (simulated)"
        FUTURES = "futures", "Futures"

    class Route(models.TextChoices):
        BOT = "bot", "Bot"
        CASCADE = "cascade", "Cascade"
        PAPER = "paper", "Paper trading"
        GATE = "gate", "Gate.io (made outside QF-gate)"
        MANUAL = "manual", "Manual entry"
        TRANSFER = "transfer", "Deposit / withdrawal"

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.TRADE)
    market = models.CharField(max_length=8, choices=Market.choices, default=Market.SPOT)
    base = models.CharField(max_length=20, help_text="Coin bought / sold / moved, e.g. ETH")
    quote = models.CharField(max_length=20, default="USDT", blank=True, help_text="Priced in, e.g. USDT")
    coin = models.ForeignKey(Coin, null=True, blank=True, on_delete=models.SET_NULL, related_name="ledger_rows")
    side = models.CharField(max_length=4, blank=True, help_text="buy | sell (trades); in | out (adjustments)")
    qty = ExactDecimalField(help_text="Base quantity of the order (before a fee taken in the coin)")
    price = ExactDecimalField(null=True, blank=True, help_text="Quote per base unit")
    fee = ExactDecimalField(default="0")
    fee_currency = models.CharField(max_length=20, blank=True)
    fee_quote = ExactDecimalField(null=True, blank=True, help_text="The fee's value in the quote currency")
    estimated = models.BooleanField(default=False, help_text="A value here was estimated (fee in GT/points, deposit cost)")
    executed_at = models.DateTimeField(db_index=True)
    route = models.CharField(max_length=10, choices=Route.choices, default=Route.MANUAL)
    source = models.CharField(max_length=120, blank=True, help_text="What made it, e.g. Bot #3 · ETH zones")
    source_key = models.CharField(max_length=120, help_text="Unique per account: re-syncs never double a row")
    order_id = models.CharField(max_length=64, blank=True, db_index=True)
    provisional = models.BooleanField(default=False, help_text="Live bot order waiting to be replaced by Gate's exact trades")
    raw = models.JSONField(default=dict, blank=True, help_text="The original record, for checking against the source")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-executed_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["account", "source_key"], name="ledger_unique_source")]
        indexes = [models.Index(fields=["account", "base", "executed_at"])]

    def __str__(self) -> str:
        return f"{self.executed_at:%Y-%m-%d %H:%M} {self.side or self.kind} {self.qty} {self.base}"

    @property
    def pair(self) -> str:
        return f"{self.base}_{self.quote}" if self.quote else self.base

    @property
    def amount_quote(self):
        """Quote value of the order before fees (qty x price)."""
        return None if self.price is None else self.qty * self.price


class SyncState(models.Model):
    """Progress of one Gate.io history stream, so each run continues where the last one stopped."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="sync_states")
    stream = models.CharField(max_length=24)  # spot_trades | deposits | withdrawals
    newest_done = models.DateTimeField(null=True, blank=True, help_text="Everything up to here is imported")
    oldest_done = models.DateTimeField(null=True, blank=True, help_text="Backfill has reached back to here")
    backfill_complete = models.BooleanField(default=False)
    rows = models.PositiveIntegerField(default=0)
    last_run = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "stream"], name="ledger_one_state_per_stream")]


class BalanceCheck(models.Model):
    """The ledger's coin totals compared with the exchange's real balances."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="balance_checks")
    at = models.DateTimeField(default=timezone.now)
    ok = models.BooleanField(default=False)
    rows = models.JSONField(default=list, help_text="[{currency, ledger, exchange, diff}] as decimal strings")
    message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-at"]
