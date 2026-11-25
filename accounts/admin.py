from django.contrib import admin

from .models import ExchangeKey


@admin.register(ExchangeKey)
class ExchangeKeyAdmin(admin.ModelAdmin):
    """Shows who has a key and its state; the encrypted values are never displayed or editable here."""

    list_display = ("owner", "exchange", "label", "key_last4", "is_active", "verified_at", "created_at")
    list_filter = ("exchange", "is_active")
    exclude = ("api_key_enc", "secret_enc")
    readonly_fields = ("owner", "exchange", "key_last4", "permissions", "verified_at", "last_error", "created_at")

    def has_add_permission(self, request):
        return False  # keys are added by their owner on the API keys page (so they get encrypted and tested)
