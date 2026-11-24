from django.contrib import admin

from .models import Account, BalanceCheck, SyncState, Transaction


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "exchange", "sync", "history_start")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("executed_at", "account", "route", "kind", "side", "base", "quote", "qty", "price", "fee",
                    "fee_currency", "provisional")
    list_filter = ("account", "route", "kind", "market", "side", "provisional", "estimated")
    search_fields = ("base", "source", "source_key", "order_id", "note")
    date_hierarchy = "executed_at"


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ("account", "stream", "newest_done", "oldest_done", "backfill_complete", "rows", "last_run")


@admin.register(BalanceCheck)
class BalanceCheckAdmin(admin.ModelAdmin):
    list_display = ("account", "at", "ok", "message")
