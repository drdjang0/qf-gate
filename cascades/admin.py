from django.contrib import admin

from .models import Cascade, CascadeBacktest, CascadeEvent, CascadeLevel


class LevelInline(admin.TabularInline):
    model = CascadeLevel
    extra = 0


@admin.register(Cascade)
class CascadeAdmin(admin.ModelAdmin):
    list_display = ("id", "coin", "kind", "mode", "status", "trigger_price", "last_price", "created_at")
    list_filter = ("status", "kind", "mode")
    inlines = [LevelInline]


@admin.register(CascadeEvent)
class CascadeEventAdmin(admin.ModelAdmin):
    list_display = ("at", "cascade", "level", "kind", "price", "message")
    list_filter = ("kind",)


@admin.register(CascadeBacktest)
class CascadeBacktestAdmin(admin.ModelAdmin):
    list_display = ("id", "coin", "kind", "interval", "start", "end", "status", "fills", "roi_pct", "created_at")
