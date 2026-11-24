from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from django import forms

from market.models import Coin
from trading.models import INTERVAL_CHOICES

from . import ladder
from .models import Cascade

PCT_FIELDS = ("base_price", "base_amount", "price_step_pct", "amount_step_pct", "num_levels", "tp_base_pct", "tp_step_pct")


class CascadeForm(forms.Form):
    name = forms.CharField(max_length=100, required=False, help_text="Optional label")
    coin = forms.ModelChoiceField(queryset=Coin.objects.none())
    kind = forms.ChoiceField(choices=Cascade.Kind.choices, initial=Cascade.Kind.PERCENT, widget=forms.RadioSelect)
    trigger_price = forms.FloatField(required=False, min_value=0, help_text=(
        "Optional: stay inactive until the price trades down to this, then level 1 goes live"))
    fee_pct = forms.FloatField(initial=0.2, min_value=0, max_value=5, label="Fee %",
                               help_text="Per fill, deducted from what you receive (Gate spot: about 0.2)")
    # % steps
    base_price = forms.FloatField(required=False, min_value=0, label="Level 1 buy price")
    base_amount = forms.FloatField(required=False, min_value=0, label="Level 1 amount (USDT)")
    price_step_pct = forms.FloatField(required=False, initial=0.5, min_value=0, max_value=99.99,
                                      label="Price step %", help_text="Each level this % below the previous one")
    amount_step_pct = forms.FloatField(required=False, initial=20, min_value=-99.99, max_value=1000,
                                       label="Amount step %", help_text="Each level's amount this % larger than the previous")
    num_levels = forms.IntegerField(required=False, initial=5, min_value=1, max_value=100, label="Levels")
    tp_base_pct = forms.FloatField(required=False, initial=5, label="Take-profit %", help_text="Level 1 sells at +this %")
    tp_step_pct = forms.FloatField(required=False, initial=1, label="Take-profit step %",
                                   help_text="Added per level: base 5, step 1 = +5%, +6%, +7%...")
    # manual template
    levels_text = forms.CharField(required=False, label="Levels", widget=forms.Textarea(attrs={
        "rows": 6, "placeholder": "one level per line: buy price, USDT amount, sell price\n0.0722, 10, 0.0758\n0.0710, 12, 0.0752"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["coin"].queryset = Coin.objects.order_by("symbol")
        self._levels: list[dict] = []

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        try:
            if data.get("kind") == Cascade.Kind.MANUAL:
                self._levels = ladder.normalize_manual(ladder.parse_manual_text(data.get("levels_text", "")))
            else:
                missing = [self.fields[f].label for f in PCT_FIELDS if data.get(f) is None]
                if missing:
                    raise forms.ValidationError("Fill in: " + ", ".join(missing))
                self._levels = ladder.compute_levels(
                    data["base_price"], data["base_amount"], data["price_step_pct"], data["amount_step_pct"],
                    data["num_levels"], data["tp_base_pct"], data["tp_step_pct"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from None
        return data

    def levels(self) -> list[dict]:
        return self._levels

    def config(self) -> dict:
        d = self.cleaned_data
        if d["kind"] == Cascade.Kind.MANUAL:
            return {"levels_text": d.get("levels_text", "")}
        return {f: d[f] for f in PCT_FIELDS}


def _utc(d, end=False) -> datetime:
    return datetime.combine(d, time.max if end else time.min, tzinfo=timezone.utc)


class CascadeBacktestForm(CascadeForm):
    interval = forms.ChoiceField(choices=INTERVAL_CHOICES, initial="5m",
                                 help_text="Shorter bars = more exact fills, slower to run")
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), help_text="UTC")
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), help_text="UTC, inclusive")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = datetime.now(timezone.utc).date()
        self.fields["start"].initial = today - timedelta(days=30)
        self.fields["end"].initial = today

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            raise forms.ValidationError("The end date is before the start date")
        return data

    def period(self) -> tuple[datetime, datetime]:
        return _utc(self.cleaned_data["start"]), _utc(self.cleaned_data["end"], end=True)
