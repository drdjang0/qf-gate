from __future__ import annotations

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from trading.models import TradingControl


class KeyForm(forms.Form):
    label = forms.CharField(required=False, max_length=60, widget=forms.TextInput(attrs={"placeholder": "e.g. main account"}))
    api_key = forms.CharField(label="API key", max_length=200, widget=forms.TextInput(attrs={"autocomplete": "off"}))
    api_secret = forms.CharField(label="API secret", max_length=200,
                                 widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}, render_value=False))
    test = forms.BooleanField(label="Test the key with Gate.io before saving", required=False, initial=True)


class TradingSettingsForm(forms.ModelForm):
    max_daily_loss_pct = forms.FloatField(label="Daily loss limit (% of bot capital)", required=False, min_value=0,
                                          help_text="Empty or 0 = no daily loss limit")
    max_exposure = forms.FloatField(label="Max open exposure (quote currency)", required=False, min_value=0,
                                    help_text="Empty or 0 = no limit")
    max_orders_per_hour = forms.IntegerField(label="Max orders per hour", required=False, min_value=0,
                                             help_text="Empty or 0 = no limit")

    class Meta:
        model = TradingControl
        fields = ["strategies_enabled", "require_checks", "min_dry_run_days", "max_daily_loss_pct", "max_exposure",
                  "max_orders_per_hour"]
        labels = {"strategies_enabled": "Use strategies and bots",
                  "require_checks": "Strategy checks before a live bot",
                  "min_dry_run_days": "Dry-run days required (when checks are on)"}

    def clean(self):
        d = super().clean()
        d["max_daily_loss_pct"] = d.get("max_daily_loss_pct") or None
        d["max_exposure"] = d.get("max_exposure") or 0.0
        d["max_orders_per_hour"] = d.get("max_orders_per_hour") or 0
        return d


class NewUserForm(UserCreationForm):
    email = forms.EmailField(required=False)
    is_staff = forms.BooleanField(label="Admin (can manage users and coins)", required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "")
        user.is_staff = self.cleaned_data.get("is_staff", False)
        if commit:
            user.save()
        return user
