"""Market data models.

Design notes
- A coin's *identity* is (chain, contract address), not its ticker symbol.
  Tickers collide across chains and exchanges; the contract address does not.
  Native coins (BTC on the BTC chain, ETH on ETH, ...) have no contract address
  and are identified by their chain instead.
- `gate_currency` is Gate.io's own currency code for that token. It usually
  equals the symbol but not always (Gate renames clashing tickers), so it is
  stored separately and verified against the contract address.
- Candles are stored per interval with a unique (coin, interval, open_time) key,
  so every sync is an idempotent upsert and re-running never duplicates data.
- All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

PRICE = {"max_digits": 32, "decimal_places": 16}
VOLUME = {"max_digits": 38, "decimal_places": 12}


def normalize_contract(address: str) -> str:
    """Comparison key for a contract address.

    EVM-style hex addresses (0x...) are case-insensitive (the mixed case is only
    a checksum), so they are lower-cased. Other formats such as Solana base58
    ARE case-sensitive and are kept as-is.
    """
    address = (address or "").strip()
    if address.lower().startswith("0x"):
        return address.lower()
    return address


class Interval(models.TextChoices):
    M1 = "1m", "1 minute"
    M5 = "5m", "5 minutes"
    M15 = "15m", "15 minutes"
    H1 = "1h", "1 hour"
    D1 = "1d", "1 day"


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


class Coin(models.Model):
    class SyncStatus(models.TextChoices):
        IDLE = "idle", "Idle"
        RUNNING = "running", "Running"
        OK = "ok", "OK"
        ERROR = "error", "Error"

    symbol = models.CharField(max_length=32, help_text="Ticker shown to you, e.g. PEPE")
    name = models.CharField(max_length=128, blank=True)
    chain = models.CharField(
        max_length=32,
        help_text="Gate.io chain code, e.g. ETH, BSC, SOL, BTC. For a native coin use its own chain.",
    )
    contract_address = models.CharField(
        max_length=128,
        blank=True,
        help_text="Token contract address on that chain. Leave blank only for native coins (BTC, ETH, SOL...).",
    )
    contract_key = models.CharField(max_length=128, blank=True, editable=False, db_index=True)
    gate_currency = models.CharField(
        max_length=32,
        blank=True,
        help_text="Gate.io currency code. Auto-filled from the contract address when left blank.",
    )
    quote = models.CharField(max_length=16, default="USDT")
    is_active = models.BooleanField(default=True, help_text="Inactive coins are not synced or streamed.")
    notes = models.TextField(blank=True)
    # filled automatically (services/coininfo.py): CoinGecko icon / id / exchanges, Gate.io chains
    image_url = models.CharField(max_length=300, blank=True)
    coingecko_id = models.CharField(max_length=100, blank=True)
    exchanges = models.JSONField(default=list, blank=True, help_text="Exchanges listing the coin (CoinGecko)")
    chains = models.JSONField(default=list, blank=True, help_text="Chains Gate.io supports, with contracts")

    verified_at = models.DateTimeField(null=True, blank=True)
    verification_note = models.CharField(max_length=255, blank=True)

    sync_status = models.CharField(max_length=16, choices=SyncStatus.choices, default=SyncStatus.IDLE)
    sync_message = models.TextField(blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol", "chain"]
        constraints = [
            models.UniqueConstraint(
                fields=["chain", "contract_key"],
                condition=~Q(contract_key=""),
                name="uniq_coin_chain_contract",
            ),
            models.UniqueConstraint(
                fields=["chain"],
                condition=Q(contract_key=""),
                name="uniq_native_coin_per_chain",
            ),
            models.UniqueConstraint(
                fields=["gate_currency", "quote"],
                condition=~Q(gate_currency=""),
                name="uniq_gate_pair",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} ({self.chain})"

    @property
    def is_native(self) -> bool:
        return not normalize_contract(self.contract_address)

    @property
    def pair(self) -> str:
        """Gate.io spot pair, e.g. BTC_USDT."""
        return f"{(self.gate_currency or self.symbol).upper()}_{self.quote.upper()}"

    def clean(self) -> None:
        self.symbol = (self.symbol or "").strip().upper()
        self.chain = (self.chain or "").strip().upper()
        self.quote = (self.quote or "USDT").strip().upper()
        self.gate_currency = (self.gate_currency or "").strip().upper()
        self.contract_address = (self.contract_address or "").strip()
        if " " in self.contract_address:
            raise ValidationError({"contract_address": "Contract address must not contain spaces."})

    def save(self, *args, **kwargs) -> None:
        self.contract_key = normalize_contract(self.contract_address)
        super().save(*args, **kwargs)


class Candle(models.Model):
    class Source(models.TextChoices):
        REST = "rest", "REST API"
        WS = "ws", "WebSocket"
        ARCHIVE = "archive", "Trade archive"

    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="candles")
    interval = models.CharField(max_length=4, choices=Interval.choices)
    open_time = models.DateTimeField(help_text="Candle open time, UTC")
    open = models.DecimalField(**PRICE)
    high = models.DecimalField(**PRICE)
    low = models.DecimalField(**PRICE)
    close = models.DecimalField(**PRICE)
    volume_base = models.DecimalField(**VOLUME, default=Decimal(0))
    volume_quote = models.DecimalField(**VOLUME, default=Decimal(0))
    # order flow: base volume of trades where the TAKER bought / sold (null when unknown, e.g. REST candles)
    buy_volume = models.DecimalField(**VOLUME, null=True, blank=True)
    sell_volume = models.DecimalField(**VOLUME, null=True, blank=True)
    is_closed = models.BooleanField(default=True, help_text="False while the candle is still forming")
    source = models.CharField(max_length=8, choices=Source.choices)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["coin", "interval", "open_time"]
        constraints = [
            models.UniqueConstraint(fields=["coin", "interval", "open_time"], name="uniq_candle"),
        ]
        indexes = [models.Index(fields=["coin", "interval", "-open_time"], name="candle_latest_idx")]

    def __str__(self) -> str:
        return f"{self.coin.symbol} {self.interval} {self.open_time:%Y-%m-%d %H:%M}"


class DailyLevel(models.Model):
    """Daily high/low (with the time each was hit), derived from stored candles.

    The high/low *price* is exact whenever the day is fully covered by candles
    of any interval aligned to the day boundary. The *time* of the high/low is
    accurate to the interval used (`time_precision`): 1m from the REST API, the
    live stream or the trade archive.
    """

    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="daily_levels")
    date = models.DateField()
    timezone_name = models.CharField(max_length=64, default="UTC")
    open = models.DecimalField(**PRICE)
    close = models.DecimalField(**PRICE)
    high = models.DecimalField(**PRICE)
    high_time = models.DateTimeField()
    low = models.DecimalField(**PRICE)
    low_time = models.DateTimeField()
    time_precision = models.CharField(max_length=4, choices=Interval.choices)
    candle_count = models.PositiveIntegerField()
    is_complete = models.BooleanField(help_text="True when the day is over and every candle is present")
    cross_check = models.CharField(
        max_length=255, blank=True, help_text="Empty when it agrees with Gate.io's own 1d candle"
    )
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["coin", "-date"]
        constraints = [
            models.UniqueConstraint(fields=["coin", "timezone_name", "date"], name="uniq_daily_level"),
        ]

    def __str__(self) -> str:
        return f"{self.coin.symbol} {self.date} H={self.high} L={self.low}"


class Job(models.Model):
    """A unit of background work, queued by the UI or the scheduler and run by `run_worker`.

    A database-backed queue keeps the stack small (no Redis/Celery) and works on
    Windows. Every run is recorded, so you can see when data was last refreshed
    and why something failed.
    """

    class Kind(models.TextChoices):
        SYNC = "sync", "Sync candles"
        VERIFY = "verify", "Verify identity"
        LEVELS = "levels", "Recompute levels"
        BACKTEST = "backtest", "Backtest"
        SWEEP = "sweep", "Parameter sweep"
        PORTFOLIO = "portfolio", "Portfolio backtest"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    class Origin(models.TextChoices):
        USER = "user", "User"
        SCHEDULE = "schedule", "Schedule"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="jobs")
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.USER)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"], name="job_queue_idx")]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.coin.symbol} [{self.status}]"

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None


class Heartbeat(models.Model):
    """Last-seen time of long-running services (worker, stream), shown in the UI."""

    name = models.CharField(max_length=32, primary_key=True)
    last_seen = models.DateTimeField()
    info = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"{self.name} @ {self.last_seen:%H:%M:%S}"

    @property
    def is_alive(self) -> bool:
        return (timezone.now() - self.last_seen).total_seconds() < 90


class FundingRate(models.Model):
    """Gate.io USDT-perpetual funding rate for a coin's contract, every 8 hours (Gate keeps 180 days)."""

    coin = models.ForeignKey(Coin, on_delete=models.CASCADE, related_name="funding_rates")
    time = models.DateTimeField(help_text="Funding time, UTC")
    rate = models.FloatField(help_text="Paid by longs to shorts when positive, per 8 h")

    class Meta:
        ordering = ["coin", "time"]
        constraints = [models.UniqueConstraint(fields=["coin", "time"], name="uniq_funding")]
