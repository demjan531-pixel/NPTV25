from django.urls import path
from . import views

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("news/", views.news_page, name="news"),
]
