"""Who may see what: every user only sees their own strategies, bots, cascades and ledger.

Market data (coins, candles, levels, news, AI briefs) is shared by everyone. Rows without an owner
(made before multi-user, or by a system task) are visible to superusers only.
"""

from __future__ import annotations

from django.contrib.auth.decorators import user_passes_test
from django.db.models import Q
from django.shortcuts import get_object_or_404

# model name -> the lookup from that model to its owner
OWNER_PATHS = {
    "strategyconfig": "owner",
    "backtestrun": "config__owner",
    "sweep": "config__owner",
    "papersession": "config__owner",
    "bot": "config__owner",
    "portfoliorun": "config__owner",
    "botorder": "bot__config__owner",
    "trade": "paper__config__owner",
    "cascade": "owner",
    "cascadebacktest": "owner",
    "cascadelevel": "cascade__owner",
    "account": "owner",
    "transaction": "account__owner",
    "exchangekey": "owner",
}


def _path(qs) -> str:
    return OWNER_PATHS[qs.model._meta.model_name]


def owned(qs, user, path: str | None = None):
    """Rows of `qs` the user may see."""
    path = path or _path(qs)
    if getattr(user, "is_superuser", False):
        return qs.filter(Q(**{path: user}) | Q(**{f"{path}__isnull": True}))
    return qs.filter(**{path: user})


def get_owned(qs_or_model, user, **lookup):
    """get_object_or_404, limited to what the user owns (404 for anyone else's)."""
    qs = qs_or_model if hasattr(qs_or_model, "model") else qs_or_model.objects.all()
    return get_object_or_404(owned(qs, user), **lookup)


staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


def operator():
    """The first superuser: owner of server-wide things (Telegram commands, legacy rows)."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("id").first()
