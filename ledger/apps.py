from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ledger"
    verbose_name = "Portfolio ledger"

    def ready(self):
        from market.services.schedule import register_periodic

        from . import gate_sync, recorder

        # every new fill from bots, paper trading and cascades goes into the ledger within a minute
        register_periodic("ledger_record", 60, recorder.record_internal)
        # the real Gate.io account: new trades / deposits / withdrawals, then older history window by window
        register_periodic("ledger_gate_sync", 300, gate_sync.sync_all)
        # once a day: the ledger's totals against Gate.io's real balances
        register_periodic("ledger_balance_check", 24 * 3600, gate_sync.check_all)
