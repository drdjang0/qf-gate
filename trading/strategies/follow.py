"""'Follow remote signals': a strategy that decides nothing itself. A bot running it executes the
signals received from another QF-gate instance for its coin (see trading/signals_share.py)."""

from __future__ import annotations

from .base import Context, Signal, Strategy, register


@register
class FollowRemote(Strategy):
    key = "follow_remote"
    name = "Follow remote signals (another QF-gate instance)"
    description = ("Executes the buy / sell signals received from another instance for this coin "
                   "(manage.py follow_signals). Backtests show no trades: the decisions are made elsewhere.")
    params = {}

    def on_bar(self, ctx: Context) -> Signal | None:
        return None
