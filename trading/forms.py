from __future__ import annotations

from datetime import timedelta

from django import forms
from django.utils import timezone

from market.models import Coin

from .models import StrategyConfig
from .strategies.base import RISK_PARAMS, Param, get_strategy


def _field(p: Param, initial):
    common = {"required": False, "help_text": p.help, "initial": initial if initial is not None else p.default}
    if p.kind == "float":
        return forms.FloatField(min_value=p.min, max_value=p.max, **common)
    if p.kind == "int":
        return forms.IntegerField(min_value=p.min, max_value=p.max, **common)
    if p.kind == "bool":
        return forms.BooleanField(**common)
    return forms.ChoiceField(choices=[(c, c) for c in p.choices], **common)


class StrategyConfigForm(forms.ModelForm):
    """Name/coin/timeframe plus fields generated from the strategy's parameter spec."""

    class Meta:
        model = StrategyConfig
        fields = ["name", "coin", "interval", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, strategy_key: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.strategy_cls = get_strategy(strategy_key)
        self.strategy_key = strategy_key
        self.fields["coin"].queryset = Coin.objects.filter(is_active=True, verified_at__isnull=False)
        self.param_names, self.risk_names = [], []
        for name, p in self.strategy_cls.params.items():
            self.fields[f"p_{name}"] = _field(p, self.instance.params.get(name) if self.instance.pk else None)
            self.fields[f"p_{name}"].label = name.replace("_", " ").capitalize()
            self.param_names.append(f"p_{name}")
        for name, p in RISK_PARAMS.items():
            self.fields[f"r_{name}"] = _field(p, self.instance.risk.get(name) if self.instance.pk else None)
            self.fields[f"r_{name}"].label = name.replace("_", " ").capitalize()
            self.risk_names.append(f"r_{name}")

    def param_fields(self):
        return [self[n] for n in self.param_names]

    def risk_fields(self):
        return [self[n] for n in self.risk_names]

    def basic_fields(self):
        return [self[n] for n in ("name", "coin", "interval", "notes")]

    def clean(self):
        data = super().clean()
        for prefix, spec, attr in (("p", self.strategy_cls.params, "_clean_params"), ("r", RISK_PARAMS, "_clean_risk")):
            cleaned = {}
            for n, p in spec.items():
                v = data.get(f"{prefix}_{n}")
                if p.kind == "bool":
                    v = bool(v)
                try:
                    cleaned[n] = p.clean(n, v)
                except ValueError as exc:
                    self.add_error(f"{prefix}_{n}", str(exc))
            setattr(self, attr, cleaned)
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.strategy_key = self.strategy_key
        obj.params = self._clean_params
        obj.risk = self._clean_risk
        if commit:
            obj.save()
        return obj


class SweepForm(forms.Form):
    """Period/validation settings plus one 'values to try' box per strategy and risk setting."""

    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    oos_pct = forms.IntegerField(
        min_value=0, max_value=60, initial=30, label="Out-of-sample %",
        help_text="The last part of the period is held back to check the ranking on unseen data (0 = off).",
    )
    rank_by = forms.ChoiceField(label="Rank by", initial="return_pct")
    min_trades = forms.IntegerField(
        min_value=0, initial=5, label="Min trades",
        help_text="Combinations with fewer in-sample trades are ranked last (too few to trust).",
    )
    method = forms.ChoiceField(label="Search", initial="grid", required=False)
    budget = forms.IntegerField(min_value=10, max_value=1000, initial=200, required=False, label="Smart search runs",
                                help_text="Smart search only: how many combinations to backtest.")
    wf_folds = forms.IntegerField(min_value=0, max_value=8, initial=0, required=False, label="Walk-forward folds",
                                  help_text="0 = off. With N folds the period is cut into N+1 parts; each fold picks the "
                                            "best settings on the past and tests them on the next part.")

    def __init__(self, *args, config: StrategyConfig, **kwargs):
        from .models import Sweep
        from .sweep import param_spec

        super().__init__(*args, **kwargs)
        self.config = config
        self.fields["rank_by"].choices = Sweep.RANK_CHOICES
        self.fields["method"].choices = Sweep.METHOD_CHOICES
        today = timezone.now().date()
        self.fields["end"].initial = today
        self.fields["start"].initial = today - timedelta(days=57)
        self.spec = param_spec(config.strategy_key)
        self.grid_names = []
        for key, p in self.spec.items():
            section, name = key.split(".", 1)
            current = (config.params if section == "p" else config.risk).get(name, p.default)
            hint = {"float": "e.g. 1, 2, 3 or 0.5:3:0.5", "int": "e.g. 2, 4, 8 or 2:10:2",
                    "bool": "true, false or both", "choice": f"{', '.join(p.choices)} or all"}[p.kind]
            fname = f"g_{section}__{name}"
            self.fields[fname] = forms.CharField(
                required=False, label=name.replace("_", " ").capitalize(),
                help_text=f"{p.help} — now: {current}. Values to try: {hint}. Empty = keep current.",
                widget=forms.TextInput(attrs={"placeholder": str(current)}),
            )
            self.grid_names.append((fname, key, section))

    def strategy_grid_fields(self):
        return [self[f] for f, _, s in self.grid_names if s == "p"]

    def risk_grid_fields(self):
        return [self[f] for f, _, s in self.grid_names if s == "r"]

    def clean(self):
        from .sweep import MAX_COMBOS, MAX_SPACE, build_grid, combo_count

        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] >= data["end"]:
            raise forms.ValidationError("Start must be before end.")
        raw = {key: data.get(fname) or "" for fname, key, _ in self.grid_names}
        try:
            grid = build_grid(self.config.strategy_key, raw)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        data["method"] = data.get("method") or "grid"
        n = combo_count(grid)
        if n < 2:
            raise forms.ValidationError("Give at least one setting two or more values to compare.")
        if data.get("method") == "smart":
            if n > MAX_SPACE:
                raise forms.ValidationError(f"That is {n:,} combinations; smart search handles up to {MAX_SPACE:,}.")
            data["budget"] = min(data.get("budget") or 200, n)
            runs = data["budget"]
        else:
            if n > MAX_COMBOS:
                raise forms.ValidationError(f"That is {n} combinations; the maximum is {MAX_COMBOS}. Use fewer values, "
                                            "or choose Smart search.")
            runs = n
        data["grid"], data["combos"], data["runs"] = grid, n, runs * (1 + (data.get("wf_folds") or 0))
        return data


class BacktestForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), help_text="Inclusive; today = up to now")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.now().date()
        self.fields["end"].initial = today
        self.fields["start"].initial = today - timedelta(days=57)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] >= data["end"]:
            raise forms.ValidationError("Start must be before end.")
        return data
