from django.urls import path

from . import views

app_name = "intel"

urlpatterns = [
    path("", views.index, name="index"),
    path("news/refresh/", views.news_refresh, name="news_refresh"),
    path("macro/refresh/", views.macro_refresh, name="macro_refresh"),
    path("card/", views.card, name="card"),
    path("briefs/new/", views.brief_create, name="brief_create"),
    path("briefs/<int:pk>/", views.brief_detail, name="brief_detail"),
    path("briefs/<int:pk>/recheck/", views.brief_recheck, name="brief_recheck"),
    path("notes/", views.note_list, name="note_list"),
    path("notes/new/", views.note_create, name="note_create"),
    path("notes/<int:pk>/", views.note_detail, name="note_detail"),
]
