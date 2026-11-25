def trading_settings(request):
    """The signed-in user's switches, for the header (hide Strategies / Bots when they are off)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from trading.models import TradingControl

    try:
        return {"qf_control": TradingControl.for_user(user)}
    except Exception:  # noqa: BLE001 - never break a page over the header
        return {}
