from django.urls import path

from . import views

app_name = "market"

urlpatterns = [
    path("", views.CoinListView.as_view(), name="coin_list"),
    path("coins/new/", views.CoinCreateView.as_view(), name="coin_create"),
    path("coins/lookup/", views.contract_lookup, name="contract_lookup"),
    path("coins/autofill.json", views.coin_autofill, name="coin_autofill"),
    path("coins/<int:pk>/enrich/", views.coin_enrich, name="coin_enrich"),
    path("coins/<int:pk>/", views.coin_detail, name="coin_detail"),
    path("coins/<int:pk>/edit/", views.CoinUpdateView.as_view(), name="coin_update"),
    path("coins/<int:pk>/delete/", views.CoinDeleteView.as_view(), name="coin_delete"),
    path("coins/<int:pk>/sync/", views.coin_sync, name="coin_sync"),
    path("coins/<int:pk>/verify/", views.coin_verify, name="coin_verify"),
    path("coins/<int:pk>/repair/", views.coin_repair, name="coin_repair"),
    path("coins/<int:pk>/candles.json", views.candles_json, name="candles_json"),
    path("coins/<int:pk>/book.json", views.book_json, name="book_json"),
    path("market/", views.market_page, name="market_page_default"),
    path("market/<int:pk>/", views.market_page, name="market_page"),
    path("market/<int:pk>/zones/", views.zones_page, name="zones_page"),
    path("market/<int:pk>/zones.xlsx", views.zones_export, name="zones_export"),
    path("screener/", views.screener_page, name="screener"),
    path("scanner/", views.scanner_page, name="scanner"),
    path("coins/<int:pk>/compare/", views.compare_page, name="compare_page"),
    path("jobs/", views.JobListView.as_view(), name="job_list"),
    path("export/levels.xlsx", views.export_levels, name="export_levels"),
]
