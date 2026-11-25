from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("keys/", views.keys_page, name="keys"),
    path("keys/<int:pk>/<str:what>/", views.key_action, name="key_action"),
    path("users/", views.users_page, name="users"),
    path("users/<int:pk>/<str:what>/", views.user_action, name="user_action"),
]
