from django.urls import path

from . import views

app_name = "ledger"

urlpatterns = [
    path("", views.index, name="index"),
    path("transactions/", views.transactions, name="transactions"),
    path("report/", views.report, name="report"),
    path("transactions/new/", views.tx_new, name="tx_new"),
    path("transactions/<int:pk>/edit/", views.tx_edit, name="tx_edit"),
    path("transactions/<int:pk>/delete/", views.tx_delete, name="tx_delete"),
    path("accounts/", views.accounts, name="accounts"),
    path("accounts/<int:pk>/edit/", views.account_edit, name="account_edit"),
    path("accounts/<int:pk>/<str:what>/", views.account_action, name="account_action"),
]
