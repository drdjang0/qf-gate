from __future__ import annotations

from django.conf import settings
from django.db import models

from . import crypto


class ExchangeKey(models.Model):
    """A user's own exchange API key, encrypted at rest. The plain secret is never stored or shown."""

    class Exchange(models.TextChoices):
        GATE = "gate", "Gate.io"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exchange_keys")
    exchange = models.CharField(max_length=10, choices=Exchange.choices, default=Exchange.GATE)
    label = models.CharField(max_length=60, blank=True)
    api_key_enc = models.TextField()
    secret_enc = models.TextField()
    key_last4 = models.CharField(max_length=8, blank=True)
    permissions = models.JSONField(default=dict, blank=True, help_text="What the last test could do")
    is_active = models.BooleanField(default=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["owner", "exchange"], condition=models.Q(is_active=True),
                                               name="one_active_key_per_exchange")]

    def __str__(self) -> str:
        return f"{self.get_exchange_display()} key …{self.key_last4} ({self.owner})"

    def set_credentials(self, key: str, secret: str) -> None:
        key, secret = key.strip(), secret.strip()
        self.api_key_enc, self.secret_enc = crypto.encrypt(key), crypto.encrypt(secret)
        self.key_last4 = key[-4:]

    def credentials(self) -> tuple[str, str]:
        return crypto.decrypt(self.api_key_enc), crypto.decrypt(self.secret_enc)

    @classmethod
    def active_for(cls, user, exchange: str = "gate"):
        if user is None:
            return None
        return cls.objects.filter(owner=user, exchange=exchange, is_active=True).first()
