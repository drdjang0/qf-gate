from __future__ import annotations

from django import forms
from django.conf import settings
from django.utils import timezone

from .exchanges.base import ExchangeError
from .exchanges.gate import get_client
from .models import Coin
from .services.verify import verify_identity


class CoinForm(forms.ModelForm):
    skip_verification = forms.BooleanField(
        required=False,
        help_text="Save without checking chain + contract against Gate.io (not recommended).",
    )

    class Meta:
        model = Coin
        fields = ["symbol", "name", "chain", "contract_address", "gate_currency", "quote", "is_active", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, client=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client
        self.verification = None
        if not self.instance.pk:
            self.fields["quote"].initial = settings.MARKET["DEFAULT_QUOTE"]

    def clean(self):
        data = super().clean()
        if self.errors or data.get("skip_verification") or not settings.MARKET["VERIFY_ON_SAVE"]:
            return data
        try:
            res = verify_identity(
                self._client or get_client(),
                symbol=data.get("symbol", ""),
                chain=data.get("chain", ""),
                contract_address=data.get("contract_address", ""),
                gate_currency=data.get("gate_currency", ""),
                quote=data.get("quote") or "USDT",
            )
        except ExchangeError as exc:
            raise forms.ValidationError(f"Could not reach Gate.io to verify the coin: {exc}") from exc
        if not res.ok:
            raise forms.ValidationError(res.errors)
        # cleaned_data feeds construct_instance(), so the resolved code is saved
        data["gate_currency"] = res.gate_currency
        data["name"] = data.get("name") or res.name
        self.verification = res
        return data

    def save(self, commit=True):
        coin = super().save(commit=False)
        if self.verification:
            coin.verified_at = timezone.now()
            coin.verification_note = self.verification.summary
        elif self.has_changed():
            coin.verified_at = None
            coin.verification_note = "Not verified"
        if commit:
            coin.save()
        return coin
