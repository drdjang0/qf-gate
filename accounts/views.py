from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from trading.models import TradingControl

from . import crypto
from .access import staff_required
from .forms import KeyForm, NewUserForm, TradingSettingsForm
from .keys import save_key, test_gate_key
from .models import ExchangeKey


def _server_live() -> bool:
    return str(settings.TRADING.get("LIVE_TRADING")).lower() in {"1", "true", "yes", "on"}


@login_required
def settings_page(request):
    c = TradingControl.for_user(request.user)
    form = TradingSettingsForm(request.POST or None, instance=c)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "mode":
            want_live = request.POST.get("mode") == "live"
            if want_live and request.POST.get("confirm") != "LIVE":
                messages.error(request, "Type LIVE to confirm real orders with your own money.")
            elif want_live and not ExchangeKey.active_for(request.user):
                messages.error(request, "Add your Gate.io API key first (API keys page).")
            else:
                c.live_enabled = want_live
                c.save(update_fields=["live_enabled", "updated_at"])
                (messages.warning if want_live else messages.success)(
                    request, "Live trading ON: live bots and live cascades send real orders." if want_live
                    else "Dry-run only: nothing is sent to the exchange.")
            return redirect("accounts:settings")
        if form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            return redirect("accounts:settings")
    return render(request, "accounts/settings.html", {
        "c": c, "form": form, "server_live": _server_live(), "key": ExchangeKey.active_for(request.user),
    })


@login_required
def keys_page(request):
    form = KeyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            tested = test_gate_key(d["api_key"], d["api_secret"]) if d["test"] else None
            if tested is not None and not tested["spot_read"]:
                messages.error(request, f"Gate.io refused the key, so it was not saved: {tested['error']}")
            else:
                k = save_key(request.user, d["api_key"], d["api_secret"], d["label"], tested)
                msg = f"Key …{k.key_last4} saved (encrypted)."
                if tested is not None:
                    msg += " Spot read: OK." + (" Wallet read: OK." if tested["wallet_read"] else
                                                 " Wallet read: no (the portfolio can't import deposits / withdrawals).")
                messages.success(request, msg)
                return redirect("accounts:keys")
        except crypto.KeyStoreError as exc:
            messages.error(request, str(exc))
    return render(request, "accounts/keys.html", {
        "form": form, "keys": ExchangeKey.objects.filter(owner=request.user), "crypto_ready": crypto.ready(),
    })


@login_required
@require_POST
def key_action(request, pk: int, what: str):
    k = get_object_or_404(ExchangeKey, pk=pk, owner=request.user)
    if what == "test":
        try:
            key, secret = k.credentials()
            tested = test_gate_key(key, secret)
        except crypto.KeyStoreError as exc:
            messages.error(request, str(exc))
            return redirect("accounts:keys")
        from django.utils import timezone

        k.permissions = {kk: v for kk, v in tested.items() if kk != "error"}
        k.last_error = tested["error"][:255]
        k.verified_at = timezone.now() if tested["spot_read"] else k.verified_at
        k.save(update_fields=["permissions", "last_error", "verified_at", "updated_at"])
        (messages.success if tested["spot_read"] else messages.error)(
            request, "Key works." if tested["spot_read"] else f"Gate.io refused the key: {tested['error']}")
    elif what == "delete":
        k.delete()
        c = TradingControl.for_user(request.user)
        if c.live_enabled and not ExchangeKey.active_for(request.user):
            c.live_enabled = False
            c.save(update_fields=["live_enabled", "updated_at"])
            messages.warning(request, "Key removed; live trading switched off (no key left).")
        else:
            messages.success(request, "Key removed from QF-gate. Also delete it on Gate.io if you no longer use it.")
    return redirect("accounts:keys")


@staff_required
def users_page(request):
    form = NewUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        u = form.save()
        messages.success(request, f"User {u.username} created. They sign in at /login/ and start in dry-run.")
        return redirect("accounts:users")
    rows = []
    for u in User.objects.order_by("id"):
        rows.append({"u": u, "control": TradingControl.objects.filter(owner=u).first(),
                     "key": ExchangeKey.active_for(u), "configs": u.strategy_configs.count(),
                     "cascades": u.cascades.count()})
    return render(request, "accounts/users.html", {"form": form, "rows": rows})


@staff_required
@require_POST
def user_action(request, pk: int, what: str):
    u = get_object_or_404(User, pk=pk)
    if u.pk == request.user.pk:
        messages.error(request, "You can't change your own account here.")
        return redirect("accounts:users")
    if what == "toggle_active":
        u.is_active = not u.is_active
        u.save(update_fields=["is_active"])
        if not u.is_active:  # a disabled user's bots and cascades must not open new trades
            from trading import bots as bots_mod

            bots_mod.halt(f"user {u.username} disabled", by=request.user.username, owner=u)
        messages.success(request, f"{u.username} {'enabled' if u.is_active else 'disabled (new entries halted)'}.")
    elif what == "toggle_staff":
        u.is_staff = not u.is_staff
        u.save(update_fields=["is_staff"])
        messages.success(request, f"{u.username} is {'now' if u.is_staff else 'no longer'} an admin.")
    return redirect("accounts:users")
