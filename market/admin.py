from django.contrib import admin

from .models import Candle, Coin, DailyLevel, Heartbeat, Job


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "coin", "origin", "status", "started_at", "finished_at")
    list_filter = ("status", "kind", "origin")
    list_select_related = ("coin",)


@admin.register(Heartbeat)
class HeartbeatAdmin(admin.ModelAdmin):
    list_display = ("name", "last_seen", "info")


@admin.register(Coin)
class CoinAdmin(admin.ModelAdmin):
    list_display = ("symbol", "chain", "contract_address", "gate_currency", "quote", "is_active", "verified_at", "sync_status")
    list_filter = ("is_active", "chain", "sync_status")
    search_fields = ("symbol", "name", "contract_address", "gate_currency")
    readonly_fields = ("contract_key", "verified_at", "verification_note", "sync_status", "sync_message", "last_synced_at")


@admin.register(Candle)
class CandleAdmin(admin.ModelAdmin):
    list_display = ("coin", "interval", "open_time", "open", "high", "low", "close", "is_closed", "source")
    list_filter = ("interval", "source", "is_closed", "coin")
    date_hierarchy = "open_time"
    list_select_related = ("coin",)


@admin.register(DailyLevel)
class DailyLevelAdmin(admin.ModelAdmin):
    list_display = ("coin", "date", "high", "high_time", "low", "low_time", "time_precision", "is_complete", "cross_check")
    list_filter = ("coin", "time_precision", "is_complete", "timezone_name")
    date_hierarchy = "date"
