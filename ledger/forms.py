from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

from django import forms

from market.models import Coin

from .models import Account, Transaction
from .pnl import METHODS


def scope_choices(user=None) -> list[tuple[str, str]]:
    choices = [("real", "All real-money accounts"), ("sim", "All simulated accounts")]
    for a in Account.objects.filter(owner=user):
        choices.append((str(a.pk), f"{a.name} ({a.get_kind_display().lower()})"))
    return choices


class FilterForm(forms.Form):
    scope = forms.ChoiceField(label="Account", required=False)
    asset = forms.CharField(label="Coin", required=False, max_length=20,
                            widget=forms.TextInput(attrs={"placeholder": "e.g. ETH (empty = all)"}))
    start = forms.DateField(label="From (UTC)", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="To (UTC, inclusive)", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    method = forms.ChoiceField(label="P&L method", choices=list(METHODS.items()), required=False)
    side = forms.ChoiceField(required=False, choices=[("", "Any side"), ("buy", "Buys"), ("sell", "Sells")])
    route = forms.ChoiceField(required=False, choices=[("", "Any route")] + list(Transaction.Route.choices))
    kind = forms.ChoiceField(required=False, choices=[("", "Any kind")] + list(Transaction.Kind.choices))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scope"].choices = scope_choices(user)

    def period(self) -> tuple[datetime | None, datetime | None]:
        d = self.cleaned_data
        start = datetime.combine(d["start"], time.min, tzinfo=timezone.utc) if d.get("start") else None
        end = (datetime.combine(d["end"], time.min, tzinfo=timezone.utc) + timedelta(days=1)) if d.get("end") else None
        return start, end


class TransactionForm(forms.ModelForm):
    executed_at = forms.DateTimeField(label="When (UTC)", input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                                                                         "%Y-%m-%d %H:%M"],
                                      widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    qty = forms.DecimalField(label="Quantity (coin)", min_value=0)
    price = forms.DecimalField(label="Price (quote per coin)", required=False, min_value=0,
                               help_text="Trades: the fill price. Deposits / adjustments: the cost price (empty = unknown)")
    fee = forms.DecimalField(required=False, min_value=0, initial=0)
    fee_quote = forms.DecimalField(label="Fee value in the quote currency", required=False, min_value=0,
                                   help_text="Only needed when the fee was paid in a third coin (e.g. GT)")

    class Meta:
        model = Transaction
        fields = ["account", "kind", "market", "side", "base", "quote", "qty", "price", "fee", "fee_currency",
                  "fee_quote", "executed_at", "note"]
        labels = {"base": "Coin", "quote": "Quote currency", "fee_currency": "Fee currency"}
        widgets = {"side": forms.Select(choices=[("buy", "Buy"), ("sell", "Sell"), ("in", "In (adjustment)"),
                                                 ("out", "Out (adjustment)")])}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(owner=user)

    def clean(self):
        d = super().clean()
        kind, side = d.get("kind"), d.get("side")
        if kind == "trade":
            if side not in ("buy", "sell"):
                self.add_error("side", "A trade is a buy or a sell.")
            if not d.get("price"):
                self.add_error("price", "A trade needs its price.")
        elif kind == "deposit":
            d["side"] = "in"
        elif kind == "withdrawal":
            d["side"] = "out"
        elif side not in ("in", "out"):
            self.add_error("side", "An adjustment is In or Out.")
        if d.get("executed_at") and d["executed_at"].tzinfo is None:
            d["executed_at"] = d["executed_at"].replace(tzinfo=timezone.utc)
        for f in ("base", "quote", "fee_currency"):
            d[f] = (d.get(f) or "").strip().upper()
        d["fee"] = d.get("fee") or 0
        fc = d["fee_currency"]
        if d.get("fee") and not fc:
            self.add_error("fee_currency", "Which currency was the fee paid in?")
        if kind == "trade" and d.get("fee") and fc and fc not in (d["base"], d["quote"]) and d.get("fee_quote") is None \
                and fc != "POINT":
            self.add_error("fee_quote", f"Enter what the {fc} fee was worth in {d['quote']}.")
        return d

    def save(self, commit=True):
        tx = super().save(commit=False)
        d = self.cleaned_data
        tx.side, tx.base, tx.quote, tx.fee_currency = d["side"], d["base"], d["quote"], d["fee_currency"]
        tx.executed_at = d["executed_at"]
        fc = tx.fee_currency
        if tx.kind == "trade" and tx.fee:
            if fc == tx.quote:
                tx.fee_quote = tx.fee
            elif fc == tx.base:
                tx.fee_quote = tx.fee * tx.price
            elif fc == "POINT" and tx.fee_quote is None:
                tx.fee_quote, tx.estimated = tx.fee, True
        if not tx.pk:
            tx.route = Transaction.Route.MANUAL if tx.kind == "trade" else Transaction.Route.TRANSFER
            tx.source = "Manual entry"
            tx.source_key = f"manual:{uuid.uuid4().hex}"
            if Coin.objects.filter(symbol__iexact=tx.base, quote__iexact=tx.quote).exists():
                tx.coin = Coin.objects.filter(symbol__iexact=tx.base, quote__iexact=tx.quote).first()
        if commit:
            tx.save()
        return tx


class CostPriceForm(forms.Form):
    price = forms.DecimalField(label="Cost price (quote per coin)", required=False, min_value=0)
    note = forms.CharField(required=False, max_length=255)


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["name", "kind", "exchange", "sync", "history_start", "note"]
        widgets = {"history_start": forms.DateInput(attrs={"type": "date"})}
