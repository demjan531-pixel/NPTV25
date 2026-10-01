import json
from datetime import timedelta

from django.utils import timezone
from django.contrib.auth.models import User

from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async

from .models import Message, UserProfile


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.room_group_name = "chat_room"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                "error": "Неверный формат сообщения."
            }))
            return

        message_text = data.get("message", "").strip()
        user = self.scope["user"]

        if not user.is_authenticated:
            await self.send(text_data=json.dumps({
                "error": "Необходимо войти в аккаунт."
            }))
            return

        if not message_text:
            return

        # Получаем профиль пользователя
        profile, _ = await sync_to_async(
            UserProfile.objects.get_or_create
        )(user=user)

        # Проверка на бан
        if profile.is_banned:
            await self.send(text_data=json.dumps({
                "error": "Вы забанены."
            }))
            return

        # =========================
        # КОМАНДЫ АДМИНИСТРАТОРА
        # =========================

        if message_text.startswith("/"):

            if not user.is_superuser:
                await self.send(text_data=json.dumps({
                    "error": "У вас нет прав для выполнения этой команды."
                }))
                return

            parts = message_text.split(" ", 2)
            command = parts[0].lower()

            # /clear
            if command == "/clear":

                await sync_to_async(
                    Message.objects.all().delete
                )()

                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "chat_clear"
                    }
                )

                return

            # /ban <username>
            elif command == "/ban" and len(parts) > 1:

                target_name = parts[1]

                target_user = await sync_to_async(
                    User.objects.filter(username=target_name).first
                )()

                if target_user:

                    target_profile, _ = await sync_to_async(
                        UserProfile.objects.get_or_create
                    )(user=target_user)

                    target_profile.is_banned = True

                    await sync_to_async(
                        target_profile.save
                    )()

                    await self.send_system_message(
                        f"Пользователь {target_name} забанен."
                    )

                else:
                    await self.send_system_message(
                        f"Пользователь {target_name} не найден."
                    )

                return

            # /mute <username> <минуты>
            elif command == "/mute" and len(parts) > 2:

                target_name = parts[1]

                try:
                    minutes = int(parts[2])
                except ValueError:
                    await self.send_system_message(
                        "Количество минут должно быть числом."
                    )
                    return

                target_user = await sync_to_async(
                    User.objects.filter(username=target_name).first
                )()

                if target_user:

                    target_profile, _ = await sync_to_async(
                        UserProfile.objects.get_or_create
                    )(user=target_user)

                    target_profile.is_muted = True
                    target_profile.muted_until = (
                        timezone.now()
                        + timedelta(minutes=minutes)
                    )

                    await sync_to_async(
                        target_profile.save
                    )()

                    await self.send_system_message(
                        f"Пользователь {target_name} "
                        f"замучен на {minutes} минут."
                    )

                else:
                    await self.send_system_message(
                        f"Пользователь {target_name} не найден."
                    )

                return

            # /prefix <username> <префикс>
            elif command == "/prefix" and len(parts) > 2:

                target_name = parts[1]
                new_prefix = parts[2]

                target_user = await sync_to_async(
                    User.objects.filter(username=target_name).first
                )()

                if target_user:

                    target_profile, _ = await sync_to_async(
                        UserProfile.objects.get_or_create
                    )(user=target_user)

                    target_profile.prefix = new_prefix

                    await sync_to_async(
                        target_profile.save
                    )()

                    await self.send_system_message(
                        f'Префикс "{new_prefix}" '
                        f"установлен для {target_name}."
                    )

                else:
                    await self.send_system_message(
                        f"Пользователь {target_name} не найден."
                    )

                return

        # =========================
        # ПРОВЕРКА MUTE
        # =========================

        is_muted = await sync_to_async(
            profile.check_mute_status
        )()

        if is_muted:
            await self.send(text_data=json.dumps({
                "error": "Вы временно замучены."
            }))
            return

        # =========================
        # ОБЫЧНОЕ СООБЩЕНИЕ
        # =========================

        prefix = f"[{profile.prefix}] " if profile.prefix else ""

        full_message = (
            f"{prefix}{user.username}: {message_text}"
        )

        await sync_to_async(
            Message.objects.create
        )(
            user=user,
            content=message_text
        )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "message": full_message,
                "username": user.username,
                "prefix": profile.prefix,
            }
        )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "message": event["message"],
            "username": event.get("username", ""),
            "prefix": event.get("prefix", ""),
        }))

    async def chat_clear(self, event):
        await self.send(text_data=json.dumps({
            "type": "clear"
        }))

    async def send_system_message(self, text):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "system_message",
                "message": f"[Система]: {text}"
            }
        )

    async def system_message(self, event):
        await self.send(text_data=json.dumps({
            "message": event["message"]
        }))
