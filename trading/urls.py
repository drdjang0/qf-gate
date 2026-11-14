from django.urls import path

from . import views

app_name = "trading"

urlpatterns = [
    path("", views.strategy_list, name="strategy_list"),
    path("new/", views.strategy_create, name="strategy_create"),
    path("<int:pk>/", views.strategy_detail, name="strategy_detail"),
    path("<int:pk>/edit/", views.strategy_edit, name="strategy_edit"),
    path("<int:pk>/delete/", views.strategy_delete, name="strategy_delete"),
    path("<int:pk>/backtest/", views.backtest_create, name="backtest_create"),
    path("<int:pk>/paper/start/", views.paper_start, name="paper_start"),
    path("<int:pk>/sweep/", views.sweep_create, name="sweep_create"),
    path("<int:pk>/check/", views.strategy_check, name="strategy_check"),
    path("sweeps/<int:pk>/", views.sweep_detail, name="sweep_detail"),
    path("sweeps/<int:pk>/apply/<int:index>/", views.sweep_apply, name="sweep_apply"),
    path("backtests/<int:pk>/", views.backtest_detail, name="backtest_detail"),
    path("backtests/<int:pk>/export.xlsx", views.backtest_export, name="backtest_export"),
    path("backtests/<int:pk>/signals.csv", views.backtest_signals_csv, name="backtest_signals_csv"),
    path("paper/<int:pk>/export.xlsx", views.paper_export, name="paper_export"),
    path("paper/<int:pk>/", views.paper_detail, name="paper_detail"),
    path("paper/<int:pk>/stop/", views.paper_stop, name="paper_stop"),
    path("<int:pk>/bot/start/", views.bot_start, name="bot_start"),
    path("bots/<int:pk>/", views.bot_detail, name="bot_detail"),
    path("bots/<int:pk>/stop/", views.bot_stop, name="bot_stop"),
    path("bots/<int:pk>/exit/", views.bot_force_exit, name="bot_force_exit"),
    path("control/", views.control, name="control"),
    path("api/v1/signals", views.api_signals, name="api_signals"),
    path("api/v1/signals/ingest", views.api_ingest, name="api_ingest"),
    path("<int:pk>/portfolio/", views.portfolio_create, name="portfolio_create"),
    path("portfolios/<int:pk>/", views.portfolio_detail, name="portfolio_detail"),
]
