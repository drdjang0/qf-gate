from django.apps import AppConfig


class IntelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "intel"
    verbose_name = "Intel (Phase 7: news, macro, AI briefs)"

    def ready(self):
        from django.conf import settings

        from market.services.schedule import register_periodic

        from . import briefs, news, notes, scorecard

        minutes = settings.INTEL["NEWS_EVERY_MINUTES"]
        if minutes > 0:
            register_periodic("intel_news", minutes * 60, news.fetch_all)
        register_periodic("intel_briefs", 5, briefs.run_queued)  # starts queued briefs in a background thread
        register_periodic("intel_settle", 900, scorecard.settle_due)  # also checks invalidation levels (7.9)
        register_periodic("intel_note", 300, notes.maybe_run)  # 7.11: once a day after INTEL_NOTE_HOUR_UTC
