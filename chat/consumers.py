import json

import redis.asyncio as redis
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from .models import Message


PRESENCE_TTL = 35
PRESENCE_PREFIX = "nptv25:presence:"


class ChatConsumer(AsyncWebsocketConsumer):
    room_group_name = "nptv25_general_chat"

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        self.user = user
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self._set_presence()
        await self.accept()

        await self.broadcast_presence()

    async def disconnect(self, close_code):
        if not hasattr(self, "redis"):
            return

        try:
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )
            await self.redis.delete(self._presence_key())
            await self.broadcast_presence()
        finally:
            await self.redis.aclose()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return

        message_type = data.get("type")

        if message_type == "heartbeat":
            await self._set_presence()
            await self.broadcast_presence()
            return

        if message_type == "message":
            await self.handle_new_message(data)
            return

        if message_type == "delete_message":
            await self.handle_delete_message(data)

    async def handle_new_message(self, data):
        text = str(data.get("message", "")).strip()
        if not text:
            return
        if len(text) > 2000:
            text = text[:2000]

        message = await self.save_message(text)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "id": message["id"],
                "username": self.user.username,
                "text": text,
                "created_at": message["created_at"],
                "user_id": self.user.id,
                "is_admin": self.is_admin,
            },
        )

    async def handle_delete_message(self, data):
        if not self.is_admin:
            return

        try:
            message_id = int(data.get("id"))
        except (TypeError, ValueError):
            return

        deleted = await self.delete_message(message_id)
        if not deleted:
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message_deleted",
                "id": message_id,
            },
        )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "message",
            "id": event["id"],
            "username": event["username"],
            "text": event["text"],
            "created_at": event["created_at"],
            "is_me": event["user_id"] == self.user.id,
            "is_admin": event["is_admin"],
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            "type": "message_deleted",
            "id": event["id"],
        }))

    async def presence_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "presence",
            "users": event["users"],
        }))

    async def broadcast_presence(self):
        users = await self.get_active_users()
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "presence_message", "users": users},
        )

    async def get_active_users(self):
        keys = []
        async for key in self.redis.scan_iter(match=f"{PRESENCE_PREFIX}*"):
            keys.append(key)

        if not keys:
            return []

        values = await self.redis.mget(keys)
        users = []
        for value in values:
            if not value:
                continue
            try:
                users.append(json.loads(value))
            except (json.JSONDecodeError, TypeError):
                # Compatibility with presence entries created by the older version.
                users.append({"username": value, "is_admin": False})

        return sorted(users, key=lambda item: item["username"].lower())

    async def _set_presence(self):
        await self.redis.setex(
            self._presence_key(),
            PRESENCE_TTL,
            json.dumps({
                "username": self.user.username,
                "is_admin": self.is_admin,
            }),
        )

    @property
    def is_admin(self):
        return bool(self.user.is_staff or self.user.is_superuser)

    def _presence_key(self):
        return f"{PRESENCE_PREFIX}{self.user.id}"

    @database_sync_to_async
    def save_message(self, text):
        message = Message.objects.create(user=self.user, text=text)
        return {
            "id": message.id,
            "created_at": message.created_at.strftime("%H:%M"),
        }

    @database_sync_to_async
    def delete_message(self, message_id):
        if not self.is_admin:
            return False

        deleted, _ = Message.objects.filter(id=message_id).delete()
        return deleted > 0
