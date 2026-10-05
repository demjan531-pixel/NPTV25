from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone
import json

from .models import NewsMessage, UserProfile


class NewsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.user = user
        if await self.is_banned():
            await self.close(code=4003)
            return

        self.room_group_name = "news_global"
        await self.channel_layer.group_add(
            self.room_group_name, self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        message_type = data.get("type", "message")

        if message_type == "delete_news":
            if not await self.is_admin():
                await self.send_error("Удалять новости могут только администраторы.")
                return

            try:
                news_id = int(data.get("id"))
            except (TypeError, ValueError):
                return

            if await self.delete_news(news_id):
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {"type": "news_deleted", "id": news_id},
                )
            return

        if message_type != "message":
            return

        if not await self.is_admin():
            await self.send_error("Публиковать новости могут только администраторы.")
            return

        text = str(data.get("message", "")).strip()
        if not text:
            return

        if len(text) > 2000:
            await self.send_error("Новость не может быть длиннее 2000 символов.")
            return

        saved = await self.save_news(text)
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "news_message", **saved},
        )

    async def news_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "news",
            "id": event["id"],
            "username": event["username"],
            "prefix": event["prefix"],
            "prefix_color": event["prefix_color"],
            "text": event["text"],
            "created_at": event["created_at"],
            "is_admin": True,
        }))

    async def news_deleted(self, event):
        await self.send(text_data=json.dumps({
            "type": "news_deleted",
            "id": event["id"],
        }))

    async def send_error(self, text):
        await self.send(text_data=json.dumps({
            "type": "system",
            "message": text,
            "error": True,
        }))

    @sync_to_async
    def is_admin(self):
        return bool(self.user.is_staff or self.user.is_superuser)

    @sync_to_async
    def is_banned(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        return bool(profile.is_banned)

    @sync_to_async
    def save_news(self, text):
        news = NewsMessage.objects.create(user=self.user, content=text)
        profile, _ = UserProfile.objects.get_or_create(user=self.user)

        return {
            "id": news.id,
            "username": self.user.username,
            "prefix": profile.prefix or "",
            "prefix_color": profile.prefix_color or "",
            "text": news.content,
            "created_at": timezone.localtime(news.timestamp).strftime("%d.%m.%Y %H:%M"),
        }

    @sync_to_async
    def delete_news(self, news_id):
        deleted, _ = NewsMessage.objects.filter(id=news_id).delete()
        return deleted > 0
