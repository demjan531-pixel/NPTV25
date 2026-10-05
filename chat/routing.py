from django.urls import re_path
from .consumers import ChatConsumer
from .news_consumers import NewsConsumer

websocket_urlpatterns = [
    re_path(r"ws/chat/$", ChatConsumer.as_asgi()),
    re_path(r"ws/news/$", NewsConsumer.as_asgi()),
]
