import json
import shlex
from datetime import timedelta

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from django.utils import timezone

from .models import Message, UserProfile


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.room_group_name = "chat_global"

        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        self.user = user
        self.user_group_name = f"user_{user.id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name
        )

        await self.accept()

        await self.send_presence()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name
            )

        if hasattr(self, "user"):
            await self.send_presence()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        message_type = data.get("type", "message")

        # heartbeat
        if message_type == "heartbeat":
            await self.send(text_data=json.dumps({
                "type": "heartbeat",
                "status": "ok",
            }))
            return

        # удаление сообщения кнопкой
        if message_type == "delete_message":
            if not await self.is_admin():
                return

            message_id = data.get("id")

            try:
                message_id = int(message_id)
            except (TypeError, ValueError):
                return

            deleted = await self.delete_message(message_id)

            if deleted:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "message_deleted",
                        "id": message_id,
                    }
                )

            return

        message = str(data.get("message", "")).strip()

        if not message:
            return

        # Проверяем команды
        if message.startswith("/"):
            await self.handle_command(message)
            return

        # Проверяем mute
        muted = await self.check_muted()

        if muted:
            await self.send_error(
                "Ты не можешь писать: у тебя мут."
            )
            return

        # Обычное сообщение
        saved_message = await self.save_message(message)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "id": saved_message["id"],
                "username": saved_message["username"],
                "text": saved_message["text"],
                "created_at": saved_message["created_at"],
                "is_admin": saved_message["is_admin"],
            }
        )

    # =========================================================
    # КОМАНДЫ АДМИНА
    # =========================================================

    async def handle_command(self, raw_command):
        if not await self.is_admin():
            await self.send_error(
                "Команды доступны только администраторам."
            )
            return

        try:
            parts = shlex.split(raw_command)
        except ValueError:
            await self.send_error("Неверный формат команды.")
            return

        if not parts:
            return

        command = parts[0].lower()

        # /help
        if command == "/help":
            await self.send_admin_message(
                "Команды: "
                "/mute <username> <минуты>, "
                "/unmute <username>, "
                "/ban <username>, "
                "/unban <username>, "
                "/kick <username>, "
                "/delete <id>, "
                "/clear, "
                "/users, "
                "/online"
            )
            return

        # /mute username minutes
        if command == "/mute":
            if len(parts) != 3:
                await self.send_admin_message(
                    "Использование: /mute <username> <минуты>"
                )
                return

            username = parts[1]

            try:
                minutes = int(parts[2])
            except ValueError:
                await self.send_admin_message(
                    "Количество минут должно быть числом."
                )
                return

            if minutes <= 0:
                await self.send_admin_message(
                    "Минуты должны быть больше 0."
                )
                return

            result = await self.mute_user(username, minutes)

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"Пользователь {username} получил мут на {minutes} мин."
            )

            await self.channel_layer.group_send(
                f"user_{result['id']}",
                {
                    "type": "moderation",
                    "action": "mute",
                    "minutes": minutes,
                }
            )

            return

        # /unmute username
        if command == "/unmute":
            if len(parts) != 2:
                await self.send_admin_message(
                    "Использование: /unmute <username>"
                )
                return

            username = parts[1]

            result = await self.unmute_user(username)

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"Мут с {username} снят."
            )
            return

        # /ban username
        if command == "/ban":
            if len(parts) != 2:
                await self.send_admin_message(
                    "Использование: /ban <username>"
                )
                return

            username = parts[1]

            result = await self.ban_user(username)

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"Пользователь {username} заблокирован."
            )

            await self.channel_layer.group_send(
                f"user_{result['id']}",
                {
                    "type": "force_disconnect",
                    "reason": "Ты заблокирован."
                }
            )

            await self.send_presence()
            return

        # /unban username
        if command == "/unban":
            if len(parts) != 2:
                await self.send_admin_message(
                    "Использование: /unban <username>"
                )
                return

            username = parts[1]

            result = await self.unban_user(username)

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"Блокировка с {username} снята."
            )
            return

        # /kick username
        if command == "/kick":
            if len(parts) != 2:
                await self.send_admin_message(
                    "Использование: /kick <username>"
                )
                return

            username = parts[1]

            result = await self.find_user(username)

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.channel_layer.group_send(
                f"user_{result['id']}",
                {
                    "type": "force_disconnect",
                    "reason": "Ты был исключён администратором."
                }
            )

            await self.send_admin_message(
                f"Пользователь {username} исключён из чата."
            )
            return

        # /delete id
        if command == "/delete":
            if len(parts) != 2:
                await self.send_admin_message(
                    "Использование: /delete <id>"
                )
                return

            try:
                message_id = int(parts[1])
            except ValueError:
                await self.send_admin_message(
                    "ID сообщения должен быть числом."
                )
                return

            deleted = await self.delete_message(message_id)

            if not deleted:
                await self.send_admin_message(
                    f"Сообщение #{message_id} не найдено."
                )
                return

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "message_deleted",
                    "id": message_id,
                }
            )

            await self.send_admin_message(
                f"Сообщение #{message_id} удалено."
            )
            return

        # /clear
        if command == "/clear":
            await self.clear_messages()

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_cleared",
                }
            )

            await self.send_admin_message(
                "Чат очищен."
            )
            return

        # /users
        if command == "/users":
            users = await self.get_users()

            if not users:
                await self.send_admin_message(
                    "Пользователей нет."
                )
                return

            names = ", ".join(users)

            await self.send_admin_message(
                f"Пользователи: {names}"
            )
            return

        # /online
        if command == "/online":
            users = await self.get_online_users()

            if not users:
                await self.send_admin_message(
                    "Сейчас никто не онлайн."
                )
                return

            names = ", ".join(users)

            await self.send_admin_message(
                f"Онлайн: {names}"
            )
            return

        await self.send_admin_message(
            f"Неизвестная команда: {command}. Напиши /help"
        )

    # =========================================================
    # WEBSOCKET EVENTS
    # =========================================================

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "message",
            "id": event["id"],
            "username": event["username"],
            "text": event["text"],
            "created_at": event["created_at"],
            "is_me": event["username"] == self.user.username,
            "is_admin": event["is_admin"],
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            "type": "message_deleted",
            "id": event["id"],
        }))

    async def chat_cleared(self, event):
        await self.send(text_data=json.dumps({
            "type": "chat_cleared",
        }))

    async def moderation(self, event):
        if event["action"] == "mute":
            await self.send(text_data=json.dumps({
                "type": "moderation",
                "action": "mute",
                "minutes": event["minutes"],
            }))

    async def force_disconnect(self, event):
        await self.send(text_data=json.dumps({
            "type": "force_disconnect",
            "reason": event.get("reason", "Соединение закрыто."),
        }))

        await self.close(code=4003)

    # =========================================================
    # DATABASE
    # =========================================================

    @sync_to_async
    def is_admin(self):
        return bool(
            self.user.is_staff or
            self.user.is_superuser
        )

    @sync_to_async
    def save_message(self, text):
        message = Message.objects.create(
            user=self.user,
            content=text,
        )

        return {
            "id": message.id,
            "username": self.user.username,
            "text": message.content,
            "created_at": timezone.localtime(
                message.timestamp
            ).strftime("%H:%M"),
            "is_admin": bool(
                self.user.is_staff or
                self.user.is_superuser
            ),
        }

    @sync_to_async
    def delete_message(self, message_id):
        deleted, _ = Message.objects.filter(
            id=message_id
        ).delete()

        return deleted > 0

    @sync_to_async
    def clear_messages(self):
        Message.objects.all().delete()

    @sync_to_async
    def check_muted(self):
        profile, _ = UserProfile.objects.get_or_create(
            user=self.user
        )

        return profile.check_mute_status()

    @sync_to_async
    def find_user(self, username):
        try:
            user = User.objects.get(
                username__iexact=username
            )

            return {
                "id": user.id,
                "username": user.username,
            }

        except User.DoesNotExist:
            return None

    @sync_to_async
    def mute_user(self, username, minutes):
        try:
            user = User.objects.get(
                username__iexact=username
            )
        except User.DoesNotExist:
            return None

        profile, _ = UserProfile.objects.get_or_create(
            user=user
        )

        profile.is_muted = True
        profile.muted_until = (
            timezone.now() +
            timedelta(minutes=minutes)
        )
        profile.save(
            update_fields=[
                "is_muted",
                "muted_until",
            ]
        )

        return {
            "id": user.id,
            "username": user.username,
        }

    @sync_to_async
    def unmute_user(self, username):
        try:
            user = User.objects.get(
                username__iexact=username
            )
        except User.DoesNotExist:
            return None

        profile, _ = UserProfile.objects.get_or_create(
            user=user
        )

        profile.is_muted = False
        profile.muted_until = None
        profile.save(
            update_fields=[
                "is_muted",
                "muted_until",
            ]
        )

        return {
            "id": user.id,
            "username": user.username,
        }

    @sync_to_async
    def ban_user(self, username):
        try:
            user = User.objects.get(
                username__iexact=username
            )
        except User.DoesNotExist:
            return None

        profile, _ = UserProfile.objects.get_or_create(
            user=user
        )

        profile.is_banned = True
        profile.save(
            update_fields=["is_banned"]
        )

        return {
            "id": user.id,
            "username": user.username,
        }

    @sync_to_async
    def unban_user(self, username):
        try:
            user = User.objects.get(
                username__iexact=username
            )
        except User.DoesNotExist:
            return None

        profile, _ = UserProfile.objects.get_or_create(
            user=user
        )

        profile.is_banned = False
        profile.save(
            update_fields=["is_banned"]
        )

        return {
            "id": user.id,
            "username": user.username,
        }

    @sync_to_async
    def get_users(self):
        return list(
            User.objects
            .filter(is_active=True)
            .values_list("username", flat=True)
            .order_by("username")
        )

    @sync_to_async
    def get_online_users(self):
        # Для одной инстанции Render этого достаточно.
        # Список реального онлайн будет отправляться через presence.
        return []

    # =========================================================
    # PRESENCE
    # =========================================================

    async def send_presence(self):
        users = await self.get_presence_users()

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "presence_update",
                "users": users,
            }
        )

    async def presence_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "presence",
            "users": event["users"],
        }))

    @sync_to_async
    def get_presence_users(self):
        # Базовый вариант: пользователь считается онлайн,
        # если его WebSocket сейчас подключён.
        #
        # Для одного worker/instance Render можно использовать
        # локальный список подключений.
        return [
            {
                "username": self.user.username,
                "is_admin": bool(
                    self.user.is_staff or
                    self.user.is_superuser
                ),
            }
        ]

    # =========================================================
    # RESPONSES
    # =========================================================

    async def send_error(self, text):
        await self.send(text_data=json.dumps({
            "type": "system",
            "message": text,
            "error": True,
        }))

    async def send_admin_message(self, text):
        await self.send(text_data=json.dumps({
            "type": "system",
            "message": text,
            "admin": True,
        }))