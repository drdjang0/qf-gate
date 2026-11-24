from django.apps import AppConfig


class CascadesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cascades"
    verbose_name = "Cascades"

    def ready(self):
        from market.services.schedule import register_periodic
        from market.signals import price_tick

        from . import engine

        # safety net: the worker steps every running dry-run cascade at the live ticker price
        register_periodic("cascade_step", 30, engine.step_all)

        def _on_price(sender, coin, price, **kwargs):
            engine.on_price(coin, price)

        # low latency: the live stream steps a coin's cascades on its price ticks (throttled per cascade)
        price_tick.connect(_on_price, dispatch_uid="cascades.price", weak=False)
