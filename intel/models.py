"""Phase 7 data: news items (point-in-time), LLM call log (cost cap), AI briefs (+ their settlement)."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class NewsItem(models.Model):
    """A headline or post, stored with the time WE first saw it (decisions may only use earlier items)."""

    uid = models.CharField(max_length=64, unique=True, help_text="sha1 of the link / guid")
    source = models.CharField(max_length=64)
    title = models.CharField(max_length=500)
    summary = models.TextField(blank=True)
    url = models.URLField(max_length=1000, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, help_text="As stated by the source (not trusted for timing)")
    first_seen = models.DateTimeField(default=timezone.now, db_index=True)
    sentiment = models.CharField(max_length=8, default="neutral")  # bullish | bearish | neutral
    score = models.FloatField(default=0.0, help_text="-1 (bearish words only) .. +1 (bullish words only)")

    class Meta:
        ordering = ["-first_seen"]

    def __str__(self) -> str:
        return f"[{self.source}] {self.title[:80]}"


class LLMCall(models.Model):
    """Every language-model call: tokens, cost, outcome. Feeds the daily budget and the response cache."""

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=100)
    purpose = models.CharField(max_length=100, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.FloatField(default=0.0)
    duration_ms = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    cached = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    cache_key = models.CharField(max_length=64, blank=True, db_index=True)
    response = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class Brief(models.Model):
    """An AI coin brief: a multi-agent debate over our own data, ending in a 5-tier rating. Advisory only."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell", "Review")

    symbol = models.CharField(max_length=32, db_index=True)
    quote = models.CharField(max_length=16, default="USDT")
    exchange = models.CharField(max_length=16, default="gate")
    coin = models.ForeignKey("market.Coin", null=True, blank=True, on_delete=models.SET_NULL, related_name="briefs")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED, db_index=True)
    progress = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    inputs = models.JSONField(default=dict, blank=True, help_text="The data snapshot every agent saw")
    reports = models.JSONField(default=dict, blank=True, help_text="Each agent's text")
    rating = models.CharField(max_length=12, blank=True)
    summary = models.TextField(blank=True)
    plan = models.JSONField(default=dict, blank=True, help_text="entry / stop / target / key_risks")
    checks = models.JSONField(default=dict, blank=True, help_text="Grounding checks: unverified numbers, dropped levels")
    price = models.FloatField(null=True, blank=True, help_text="Last price when the brief was made")
    btc_price = models.FloatField(null=True, blank=True)
    calls = models.PositiveIntegerField(default=0)
    cost_usd = models.FloatField(default=0.0)
    error = models.TextField(blank=True)
    horizon_hours = models.PositiveIntegerField(default=24)
    # 7.6 settlement: what actually happened over the horizon (only live briefs exist, so no hindsight)
    settled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    settle_price = models.FloatField(null=True, blank=True)
    settle_btc_price = models.FloatField(null=True, blank=True)
    ret_pct = models.FloatField(null=True, blank=True)
    btc_ret_pct = models.FloatField(null=True, blank=True)
    alpha_pct = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Brief #{self.pk} {self.symbol}/{self.quote} {self.rating or self.status}"

    @property
    def pair(self) -> str:
        return f"{self.symbol}/{self.quote}"

    @property
    def due_at(self):
        from datetime import timedelta
        return self.created_at + timedelta(hours=self.horizon_hours)
