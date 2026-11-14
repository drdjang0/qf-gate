from django.core.management.base import CommandError
from django.db.models import Q

from market.models import Coin


def select_coins(spec: list[str] | None, active_only: bool = True):
    """Coins by id, symbol, Gate pair or contract address; all (active) coins when spec is empty."""
    qs = Coin.objects.all()
    if active_only and not spec:
        qs = qs.filter(is_active=True)
    if not spec:
        return list(qs)
    out = []
    for s in spec:
        q = Q(symbol__iexact=s) | Q(gate_currency__iexact=s.split("_")[0]) | Q(contract_address__iexact=s)
        if s.isdigit():
            q |= Q(pk=int(s))
        found = list(Coin.objects.filter(q))
        if not found:
            raise CommandError(f"No coin matches {s!r}")
        if len(found) > 1:
            raise CommandError(f"{s!r} is ambiguous ({', '.join(map(str, found))}); use the id or contract address")
        out.extend(found)
    return out
