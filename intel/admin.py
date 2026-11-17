from django.contrib import admin

from .models import Brief, DailyNote, LLMCall, NewsItem


@admin.register(NewsItem)
class NewsItemAdmin(admin.ModelAdmin):
    list_display = ("first_seen", "source", "sentiment", "title")
    list_filter = ("source", "sentiment")
    search_fields = ("title",)


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "provider", "model", "purpose", "input_tokens", "output_tokens", "cost_usd", "ok", "cached")
    list_filter = ("provider", "ok", "cached")


@admin.register(Brief)
class BriefAdmin(admin.ModelAdmin):
    list_display = ("created_at", "symbol", "quote", "exchange", "source", "status", "rating", "alpha_pct",
                    "invalidated_at", "cost_usd")
    list_filter = ("status", "rating", "source")


@admin.register(DailyNote)
class DailyNoteAdmin(admin.ModelAdmin):
    list_display = ("date", "created_at", "status", "trigger", "cost_usd")
    list_filter = ("status", "trigger")
