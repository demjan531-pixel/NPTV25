import json

import redis.asyncio as redis
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.utils.timezone import localtime

from .models import Message, UserProfile


PRESENCE_KEY = "nptv25:chat:online"
MAX_MESSAGE_LENGTH = 2000


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.user = user
        self.room_group_name = "chat_global"
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

        if await self.is_banned():
            await self.redis.aclose()
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )
        await self.accept()

        await self.redis.sadd(
            PRESENCE_KEY,
            json.dumps(
                {
                    "channel": self.channel_name,
                    "user_id": self.user.id,
                    "username": self.user.username,
                    "is_admin": self.user.is_staff or self.user.is_superuser,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        await self.broadcast_presence()

    async def disconnect(self, close_code):
        if not hasattr(self, "redis"):
            return

        try:
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )
            await self.redis.srem(
                PRESENCE_KEY,
                json.dumps(
                    {
                        "channel": self.channel_name,
                        "user_id": self.user.id,
                        "username": self.user.username,
                        "is_admin": self.user.is_staff or self.user.is_superuser,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            await self.broadcast_presence()
        finally:
            await self.redis.aclose()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (TypeError, json.JSONDecodeError):
            return

        message_type = data.get("type")

        if message_type == "heartbeat":
            await self.send(text_data=json.dumps({"type": "heartbeat_ack"}))
            return

        if message_type == "message":
            text = str(data.get("message", "")).strip()
            if not text:
                return

            text = text[:MAX_MESSAGE_LENGTH]

            allowed, reason = await self.can_send_message()
            if not allowed:
                await self.send(
                    text_data=json.dumps(
                        {"type": "error", "message": reason},
                        ensure_ascii=False,
                    )
                )
                return

            message = await self.create_message(text)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "id": message["id"],
                    "user_id": message["user_id"],
                    "username": message["username"],
                    "is_admin": message["is_admin"],
                    "text": message["text"],
                    "created_at": message["created_at"],
                },
            )
            return

        if message_type == "delete_message":
            message_id = data.get("id")
            if not isinstance(message_id, int):
                return

            if not await self.is_admin():
                return

            deleted = await self.delete_message(message_id)
            if deleted:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "message_deleted",
                        "id": message_id,
                    },
                )

    async def chat_message(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "message",
                    "id": event["id"],
                    "user_id": event["user_id"],
                    "username": event["username"],
                    "is_admin": event["is_admin"],
                    "is_me": event["user_id"] == self.user.id,
                    "text": event["text"],
                    "created_at": event["created_at"],
                },
                ensure_ascii=False,
            )
        )

    async def message_deleted(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "message_deleted",
                    "id": event["id"],
                }
            )
        )

    async def broadcast_presence(self):
        members = await self.redis.smembers(PRESENCE_KEY)
        users = {}

        for raw in members:
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue

            # Один пользователь может открыть чат в нескольких вкладках.
            users[item["user_id"]] = {
                "user_id": item["user_id"],
                "username": item["username"],
                "is_admin": item["is_admin"],
            }

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "presence_update",
                "users": sorted(
                    users.values(),
                    key=lambda user: user["username"].lower(),
                ),
            },
        )

    async def presence_update(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "presence",
                    "users": event["users"],
                },
                ensure_ascii=False,
            )
        )

    @database_sync_to_async
    def is_admin(self):
        return self.user.is_staff or self.user.is_superuser

    @database_sync_to_async
    def is_banned(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        return profile.is_banned

    @database_sync_to_async
    def can_send_message(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.user)

        if profile.is_banned:
            return False, "Ваш аккаунт заблокирован."

        if profile.check_mute_status():
            return False, "Вам временно запрещено отправлять сообщения."

        return True, ""

    @database_sync_to_async
    def create_message(self, text):
        message = Message.objects.create(
            user=self.user,
            content=text,
        )

        return {
            "id": message.id,
            "user_id": message.user_id,
            "username": self.user.username,
            "is_admin": self.user.is_staff or self.user.is_superuser,
            "text": message.content,
            "created_at": localtime(message.timestamp).strftime("%H:%M"),
        }

    @database_sync_to_async
    def delete_message(self, message_id):
        deleted, _ = Message.objects.filter(id=message_id).delete()
        return deleted > 0
