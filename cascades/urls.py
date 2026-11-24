from django.urls import path

from . import views

app_name = "cascades"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/<str:what>/", views.action, name="action"),
    path("<int:pk>/levels/<int:level_pk>/<str:what>/", views.level, name="level"),
    path("backtests/new/", views.backtest_create, name="backtest_create"),
    path("backtests/<int:pk>/", views.backtest_detail, name="backtest_detail"),
    path("backtests/<int:pk>/delete/", views.backtest_delete, name="backtest_delete"),
]
