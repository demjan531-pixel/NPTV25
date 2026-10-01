import json
import os
import shlex
import time
from datetime import timedelta
import re
import redis.asyncio as redis
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from django.utils import timezone

from .models import Message, UserProfile


PRESENCE_KEY = "nptv25:chat:presence"
PRESENCE_TTL = 45


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.user = user

        if await self.is_banned():
            await self.close(code=4003)
            return

        self.room_group_name = "chat_global"
        self.user_group_name = f"user_{user.id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name,
        )

        await self.accept()

        await self.presence_add()
        await self.broadcast_presence()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name,
            )

        if hasattr(self, "user"):
            await self.presence_remove()
            await self.broadcast_presence()

    # =========================================================
    # RECEIVE
    # =========================================================

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        message_type = data.get("type", "message")

        # -----------------------------------------------------
        # HEARTBEAT
        # -----------------------------------------------------

        if message_type == "heartbeat":
            await self.presence_refresh()

            await self.send(text_data=json.dumps({
                "type": "heartbeat",
                "status": "ok",
            }))

            return

        # -----------------------------------------------------
        # DELETE MESSAGE
        # -----------------------------------------------------

        if message_type == "delete_message":

            if not await self.is_admin():
                await self.send_error(
                    "У тебя нет прав администратора."
                )
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
                    },
                )

            return

        # -----------------------------------------------------
        # MESSAGE
        # -----------------------------------------------------

        message = str(
            data.get("message", "")
        ).strip()

        if not message:
            return

        # Команды
        if message.startswith("/"):
            await self.handle_command(message)
            return

        # Проверка мута
        if await self.check_muted():
            await self.send_error(
                "Ты не можешь писать: у тебя мут."
            )
            return

        # Антиспам
        if not await self.check_antispam(message):
            return

        # Сохраняем сообщение
        saved = await self.save_message(message)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                **saved,
            },
        )

    # =========================================================
    # ADMIN COMMANDS
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
            await self.send_error(
                "Неверный формат команды."
            )
            return

        if not parts:
            return

        command = parts[0].lower()

        # -----------------------------------------------------
        # HELP
        # -----------------------------------------------------

        if command == "/help":
            await self.send_admin_message(
                "Команды: "
                "/mute <username> <минуты> | "
                "/unmute <username> | "
                "/ban <username> | "
                "/unban <username> | "
                "/kick <username> | "
                "/delete <id> | "
                "/clear | "
                "/prefix <username> <префикс> <цвет> | "
                "/unprefix <username> | "
                "/users | "
                "/online"
            )
            return

        # -----------------------------------------------------
        # PREFIX
        # -----------------------------------------------------

        if command == "/prefix":
            if len(parts) < 4:
                await self.send_admin_message(
                    'Использование: /prefix <username> <prefix> <color>'
                )
                return

            username = parts[1]
            prefix = parts[2]
            color = parts[3].lower()

            if len(prefix) > 50:
                await self.send_admin_message(
                    "Префикс не может быть длиннее 50 символов."
                )
                return

            allowed_colors = {
                "red", "blue", "green", "yellow", "orange", "purple",
                "pink", "cyan", "white", "black", "gray", "grey",
                "gold", "lime", "aqua", "magenta",
            }

            if color not in allowed_colors and not re.fullmatch(
                r"#[0-9a-fA-F]{6}", color
            ):
                await self.send_admin_message(
                    "Недопустимый цвет. Например: red, blue или #ff0000."
                )
                return

            result = await self.set_prefix(
                username,
                prefix,
                color,
            )

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"✓ Префикс {username} изменён: [{prefix}] ({color})"
            )

            await self.broadcast_presence()
            return

        # -----------------------------------------------------
        # REMOVE PREFIX
        # -----------------------------------------------------

        if command == "/unprefix":

            if len(parts) != 2:
                await self.send_admin_message(
                    "Использование: /unprefix <username>"
                )
                return

            username = parts[1]

            result = await self.set_prefix(
                username,
                "",
                "",
            )

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"✓ Префикс с {username} снят."
            )

            await self.broadcast_presence()

            return

        # -----------------------------------------------------
        # MUTE
        # -----------------------------------------------------

        if command == "/mute":

            if len(parts) != 3:
                await self.send_admin_message(
                    "/mute <username> <минуты>"
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

            result = await self.mute_user(
                username,
                minutes,
            )

            if not result:
                await self.send_admin_message(
                    f"Пользователь {username} не найден."
                )
                return

            await self.send_admin_message(
                f"✓ {username} получил мут на {minutes} мин."
            )

            await self.channel_layer.group_send(
                f"user_{result['id']}",
                {
                    "type": "moderation",
                    "action": "mute",
                    "minutes": minutes,
                },
            )

            return

        # -----------------------------------------------------
        # UNMUTE
        # -----------------------------------------------------

        if command == "/unmute":

            if len(parts) != 2:
                await self.send_admin_message(
                    "/unmute <username>"
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
                f"✓ Мут с {username} снят."
            )

            return

        # -----------------------------------------------------
        # BAN
        # -----------------------------------------------------

        if command == "/ban":

            if len(parts) != 2:
                await self.send_admin_message(
                    "/ban <username>"
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
                f"✓ {username} заблокирован."
            )

            await self.channel_layer.group_send(
                f"user_{result['id']}",
                {
                    "type": "force_disconnect",
                    "reason": "Ты заблокирован.",
                },
            )

            await self.broadcast_presence()

            return

        # -----------------------------------------------------
        # UNBAN
        # -----------------------------------------------------

        if command == "/unban":

            if len(parts) != 2:
                await self.send_admin_message(
                    "/unban <username>"
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
                f"✓ Блокировка с {username} снята."
            )

            return

        # -----------------------------------------------------
        # KICK
        # -----------------------------------------------------

        if command == "/kick":

            if len(parts) != 2:
                await self.send_admin_message(
                    "/kick <username>"
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
                    "reason": "Ты был исключён администратором.",
                },
            )

            await self.send_admin_message(
                f"✓ {username} исключён из чата."
            )

            return

        # -----------------------------------------------------
        # DELETE
        # -----------------------------------------------------

        if command == "/delete":

            if len(parts) != 2:
                await self.send_admin_message(
                    "/delete <id>"
                )
                return

            try:
                message_id = int(parts[1])
            except ValueError:
                await self.send_admin_message(
                    "ID сообщения должен быть числом."
                )
                return

            deleted = await self.delete_message(
                message_id
            )

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
                },
            )

            return

        # -----------------------------------------------------
        # CLEAR
        # -----------------------------------------------------

        if command == "/clear":

            await self.clear_messages()

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_cleared",
                },
            )

            await self.send_admin_message(
                "✓ Чат очищен."
            )

            return

        # -----------------------------------------------------
        # USERS
        # -----------------------------------------------------

        if command == "/users":

            users = await self.get_users()

            if not users:
                await self.send_admin_message(
                    "Пользователей нет."
                )
                return

            await self.send_admin_message(
                "Пользователи: " + ", ".join(users)
            )

            return

        # -----------------------------------------------------
        # ONLINE
        # -----------------------------------------------------

        if command == "/online":

            users = await self.get_online_users()

            if not users:
                await self.send_admin_message(
                    "Сейчас никто не онлайн."
                )
                return

            await self.send_admin_message(
                "Онлайн: " +
                ", ".join(
                    user["username"]
                    for user in users
                )
            )

            return

        await self.send_admin_message(
            f"Неизвестная команда: {command}. "
            "Напиши /help"
        )

    # =========================================================
    # CHAT EVENTS
    # =========================================================

    async def chat_message(self, event):

        await self.send(text_data=json.dumps({
            "type": "message",
            "id": event["id"],
            "username": event["username"],
            "prefix": event.get("prefix", ""),
            "prefix_color": event.get("prefix_color", ""),
            "text": event["text"],
            "created_at": event["created_at"],
            "is_me": (
                event["username"] ==
                self.user.username
            ),
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

        await self.send(text_data=json.dumps({
            "type": "moderation",
            "action": event["action"],
            "minutes": event.get("minutes"),
        }))

    async def force_disconnect(self, event):

        await self.send(text_data=json.dumps({
            "type": "force_disconnect",
            "reason": event.get(
                "reason",
                "Соединение закрыто.",
            ),
        }))

        await self.close(code=4003)

    async def presence_update(self, event):

        await self.send(text_data=json.dumps({
            "type": "presence",
            "users": event["users"],
        }))

    # =========================================================
    # REDIS PRESENCE
    # =========================================================

    def redis_client(self):
        url = os.environ.get("REDIS_URL")

        if not url:
            return None

        return redis.from_url(
            url,
            decode_responses=True,
        )

    async def presence_add(self):

        client = self.redis_client()

        if client is None:
            return

        try:
            now = int(time.time())

            await client.zadd(
                PRESENCE_KEY,
                {
                    self.channel_name: now,
                },
            )

            await client.hset(
                f"{PRESENCE_KEY}:users",
                self.channel_name,
                json.dumps({
                    "user_id": self.user.id,
                    "username": self.user.username,
                }),
            )

        finally:
            await client.aclose()

    async def presence_refresh(self):

        client = self.redis_client()

        if client is None:
            return

        try:
            await client.zadd(
                PRESENCE_KEY,
                {
                    self.channel_name: int(time.time()),
                },
            )
        finally:
            await client.aclose()

    async def presence_remove(self):

        client = self.redis_client()

        if client is None:
            return

        try:
            await client.zrem(
                PRESENCE_KEY,
                self.channel_name,
            )

            await client.hdel(
                f"{PRESENCE_KEY}:users",
                self.channel_name,
            )

        finally:
            await client.aclose()

    async def get_online_users(self):

        client = self.redis_client()

        if client is None:
            return []

        try:
            now = int(time.time())

            # Удаляем зависшие соединения.
            await client.zremrangebyscore(
                PRESENCE_KEY,
                0,
                now - PRESENCE_TTL,
            )

            channels = await client.zrange(
                PRESENCE_KEY,
                0,
                -1,
            )

            if not channels:
                return []

            raw_users = await client.hmget(
                f"{PRESENCE_KEY}:users",
                channels,
            )

            unique_users = {}

            for raw in raw_users:

                if not raw:
                    continue

                try:
                    user = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                unique_users[user["user_id"]] = user

            result = []

            for user in unique_users.values():

                profile = await self.get_profile_data(
                    user["user_id"]
                )

                result.append({
                    "username": user["username"],
                    "prefix": profile["prefix"],
                    "prefix_color": profile["prefix_color"],
                    "is_admin": profile["is_admin"],
                })

            result.sort(
                key=lambda x: x["username"].lower()
            )

            return result

        finally:
            await client.aclose()

    async def broadcast_presence(self):

        users = await self.get_online_users()

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "presence_update",
                "users": users,
            },
        )

    async def check_antispam(self, message):
        """
        Redis anti-spam:
        - minimum 0.8 sec between messages
        - max 5 messages per 10 sec
        - blocks exact duplicate messages for 10 sec
        """
        client = self.redis_client()

        if client is None:
            return True

        user_id = self.user.id
        cooldown_key = f"nptv25:spam:cooldown:{user_id}"
        count_key = f"nptv25:spam:count:{user_id}"
        last_key = f"nptv25:spam:last:{user_id}"

        try:
            if await client.exists(cooldown_key):
                await self.send_error(
                    "Не так быстро. Подожди немного."
                )
                return False

            count = await client.incr(count_key)

            if count == 1:
                await client.expire(count_key, 10)

            if count > 5:
                await self.send_error(
                    "Антиспам: максимум 5 сообщений за 10 секунд."
                )
                return False

            last_message = await client.get(last_key)

            if last_message == message:
                await self.send_error(
                    "Не отправляй одно и то же сообщение подряд."
                )
                return False

            await client.set(last_key, message, ex=10)
            await client.set(cooldown_key, "1", ex=1)

            return True
        finally:
            await client.aclose()

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
    def is_banned(self):
        profile, _ = UserProfile.objects.get_or_create(
            user=self.user
        )
        return bool(profile.is_banned)

    @sync_to_async
    def save_message(self, text):

        message = Message.objects.create(
            user=self.user,
            content=text,
        )

        profile, _ = UserProfile.objects.get_or_create(
            user=self.user
        )

        return {
            "id": message.id,
            "username": self.user.username,
            "prefix": profile.prefix or "",
            "prefix_color": getattr(profile, "prefix_color", "") or "",
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
    def get_profile_data(self, user_id):

        user = User.objects.get(
            id=user_id
        )

        profile, _ = UserProfile.objects.get_or_create(
            user=user
        )

        return {
            "prefix": profile.prefix or "",
            "prefix_color": getattr(profile, "prefix_color", "") or "",
            "is_admin": bool(
                user.is_staff or
                user.is_superuser
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
    def set_prefix(self, username, prefix, color=""):
        try:
            user = User.objects.get(
                username__iexact=username
            )
        except User.DoesNotExist:
            return None

        profile, _ = UserProfile.objects.get_or_create(
            user=user
        )

        profile.prefix = prefix
        if hasattr(profile, "prefix_color"):
            profile.prefix_color = color

        update_fields = ["prefix"]
        if hasattr(profile, "prefix_color"):
            update_fields.append("prefix_color")

        profile.save(update_fields=update_fields)

        return {
            "id": user.id,
            "username": user.username,
            "prefix": prefix,
            "prefix_color": color,
        }

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
            .values_list(
                "username",
                flat=True,
            )
            .order_by("username")
        )


    async def send_error(self, text):
        await self.send(
            text_data=json.dumps({
                "type": "system",
                "message": text,
                "error": True,
            })
        )

    async def send_admin_message(self, text):
        await self.send(
            text_data=json.dumps({
                "type": "system",
                "message": text,
                "admin": True,
            })
        )