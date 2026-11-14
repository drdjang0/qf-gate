from django.contrib import admin

from .models import BacktestRun, PaperSession, StrategyConfig, Trade


@admin.register(StrategyConfig)
class StrategyConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "strategy_key", "coin", "interval", "updated_at")
    list_filter = ("strategy_key", "interval")


@admin.register(BacktestRun)
class BacktestRunAdmin(admin.ModelAdmin):
    list_display = ("id", "config", "start", "end", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = ("metrics", "equity")


@admin.register(PaperSession)
class PaperSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "config", "status", "started_at", "equity", "last_bar_time")
    list_filter = ("status",)


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ("entry_time", "exit_time", "entry_price", "exit_price", "pnl", "r_multiple", "exit_reason", "backtest", "paper")
    list_filter = ("exit_reason",)
