"""Finding a user's Gate.io credentials, and testing a key before it is saved."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from .models import ExchangeKey


def gate_credentials(user) -> tuple[str, str] | None:
    """(key, secret) for the user's active Gate.io key. The operator (first superuser) may still use the
    keys in the server environment (GATE_API_KEY / GATE_API_SECRET) when no key is saved on the page."""
    k = ExchangeKey.active_for(user)
    if k is not None:
        return k.credentials()
    from .access import operator

    op = operator()
    if user is not None and op is not None and user.pk == op.pk:
        key, secret = settings.GATE.get("API_KEY"), settings.GATE.get("API_SECRET")
        if key and secret:
            return key, secret
    return None


def test_gate_key(key: str, secret: str, session=None) -> dict:
    """Ask Gate.io what this key can read. Trading permission can't be tested without an order;
    Gate reports a missing trade permission on the first order instead."""
    from ledger.gate_sync import ReadClient, SyncError

    client = ReadClient(key, secret, session=session)
    result = {"spot_read": False, "wallet_read": False, "error": ""}
    try:
        client.get("/spot/accounts")
        result["spot_read"] = True
    except SyncError as exc:
        result["error"] = str(exc)[:200]
        return result
    try:
        client.get("/wallet/deposits", {"limit": 1})
        result["wallet_read"] = True
    except SyncError as exc:
        result["error"] = f"Wallet read: {str(exc)[:160]}"
    return result


def save_key(user, key: str, secret: str, label: str = "", tested: dict | None = None) -> ExchangeKey:
    """Replace the user's active Gate.io key with a new one (the old one is deactivated, kept for history)."""
    ExchangeKey.objects.filter(owner=user, exchange="gate", is_active=True).update(is_active=False)
    k = ExchangeKey(owner=user, exchange="gate", label=label[:60])
    k.set_credentials(key, secret)
    if tested is not None:
        k.permissions = {kk: v for kk, v in tested.items() if kk != "error"}
        k.last_error = tested.get("error", "")[:255]
        k.verified_at = timezone.now() if tested.get("spot_read") else None
    k.save()
    return k
