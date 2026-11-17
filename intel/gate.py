"""7.7 AI filter for paper trading and dry-run bots (our idea; roadmap Phase 7).

A strategy's risk settings can ask for the latest AI brief on its coin to agree before a NEW entry:
`ai_min_rating` (off / buy / overweight / hold) and `ai_max_age_hours`. Rules:

- Paper sessions and dry-run bots only. Backtests and sweeps never use it (an LLM can't be replayed on the
  past without hindsight), and live bots ignore it.
- Point in time: the brief must have FINISHED before the bar closed, and be no older than the max age then.
- It acts only once the brief scorecard (7.6) has earned it; until then it blocks nothing and the paper /
  bot page says why. Exits are never blocked.
- Longs need a rating at least as bullish as the setting; shorts (futures paper) need the mirror image.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Brief
from .scorecard import SCORES, filter_earned

THRESHOLD = {"buy": 1.0, "overweight": 0.5, "hold": 0.0}


def make_gate(coin, risk: dict, mode: str):
    """Return gate(bar, signal) -> block reason or None, or None when the filter does not apply."""
    level = (risk or {}).get("ai_min_rating", "off")
    if level not in THRESHOLD or mode not in ("paper", "dry_run") or coin is None:
        return None
    need = THRESHOLD[level]
    max_age = timedelta(hours=int((risk or {}).get("ai_max_age_hours") or 24))
    symbol, quote = (coin.gate_currency or coin.symbol).upper(), coin.quote.upper()

    def gate(bar, sig):
        if not filter_earned()["ok"]:
            return None  # not earned yet: the page explains; nothing is blocked
        as_of = datetime.fromtimestamp(bar.time + bar.step, tz=timezone.utc)
        b = (Brief.objects.filter(symbol=symbol, quote=quote, status=Brief.Status.DONE,
                                  finished_at__lte=as_of, finished_at__gt=as_of - max_age)
             .order_by("-finished_at").first())
        if b is None:
            return "AI filter: no recent brief"
        score = SCORES.get(b.rating)
        if score is None:
            return "AI filter: brief needs review"
        direction = -1 if getattr(sig, "action", "buy") == "short" else 1
        if score * direction < need:
            return f"AI filter: brief rated {b.rating}"
        return None

    return gate


def status(risk: dict, mode: str) -> dict | None:
    """What the filter does for a paper session / bot, for its page (None when switched off)."""
    level = (risk or {}).get("ai_min_rating", "off")
    if level not in THRESHOLD:
        return None
    if mode not in ("paper", "dry_run"):
        return {"active": False, "level": level, "why": "ignored: the AI filter only runs in paper trading and dry-run bots"}
    earned = filter_earned()
    return {"active": earned["ok"], "level": level,
            "why": f"needs the latest brief to be {level.capitalize()} or better" if earned["ok"]
            else f"inactive until the scorecard earns it: {earned['why']}"}
