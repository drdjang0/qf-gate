from django.apps import AppConfig


class TradingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "trading"

    def ready(self):
        from market.services.jobs import register_handler
        from market.services.schedule import register_periodic
        from market.signals import candle_closed, price_tick

        from . import backtest, bots, paper, portfolio, sweep

        register_handler("backtest", backtest.handle_job)
        register_handler("sweep", sweep.handle_job)
        register_handler("portfolio", portfolio.handle_job)
        # safety net: the worker steps every running paper session once a minute
        register_periodic("paper_step", 60, paper.step_all)

        # bots: decisions once a minute as a safety net; a stream-down alert when bots depend on it
        register_periodic("bot_step", 60, bots.step_all)
        register_periodic("service_check", 60, bots.check_services)
        from market.services import funding

        register_periodic("funding_sync", 6 * 3600, funding.sync_all)

        def _on_candle_closed(sender, coin, **kwargs):
            paper.step_all(coin=coin)
            bots.step_all(coin=coin)

        def _on_price(sender, coin, price, **kwargs):
            bots.on_price(coin, price)

        # between bars: exit a bot at its stop / target on the live price
        price_tick.connect(_on_price, dispatch_uid="trading.bot_price", weak=False)

        # low latency: the live stream steps a coin's sessions as soon as a minute closes
        candle_closed.connect(_on_candle_closed, dispatch_uid="trading.paper_step", weak=False)
