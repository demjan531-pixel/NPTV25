import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .models import Message, UserProfile


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        # Название общей группы чата
        self.room_group_name = "chat_room"

        # Подключаем пользователя к группе
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        # Удаляем пользователя из группы при отключении
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({
                "error": "Некорректный формат сообщения."
            })
            return

        message_text = data.get("message", "").strip()
        user = self.scope["user"]

        if not user.is_authenticated:
            await self.send_json({
                "error": "Необходимо войти в аккаунт."
            })
            return

        if not message_text:
            return

        profile, _ = await sync_to_async(
            UserProfile.objects.get_or_create
        )(user=user)

        # Проверка бана
        if profile.is_banned:
            await self.send_json({
                "error": "Вы забанены."
            })
            return

        # Админские команды
        if message_text.startswith("/"):
            if not user.is_superuser:
                await self.send_json({
                    "error": "У вас нет прав для выполнения этой команды."
                })
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

            # /ban username
            elif command == "/ban" and len(parts) > 1:
                target_name = parts[1]

                target_user = await sync_to_async(
                    User.objects.filter(username=target_name).first
                )()

                if not target_user:
                    await self.send_json({
                        "error": f"Пользователь {target_name} не найден."
                    })
                    return

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
                return

            # /mute username minutes
            elif command == "/mute" and len(parts) > 2:
                target_name = parts[1]

                try:
                    minutes = int(parts[2])
                except ValueError:
                    await self.send_json({
                        "error": "Количество минут должно быть числом."
                    })
                    return

                if minutes <= 0:
                    await self.send_json({
                        "error": "Количество минут должно быть больше 0."
                    })
                    return

                target_user = await sync_to_async(
                    User.objects.filter(username=target_name).first
                )()

                if not target_user:
                    await self.send_json({
                        "error": f"Пользователь {target_name} не найден."
                    })
                    return

                target_profile, _ = await sync_to_async(
                    UserProfile.objects.get_or_create
                )(user=target_user)

                target_profile.is_muted = True
                target_profile.muted_until = (
                    timezone.now() + timedelta(minutes=minutes)
                )

                await sync_to_async(
                    target_profile.save
                )()

                await self.send_system_message(
                    f"Пользователь {target_name} замучен на {minutes} минут."
                )
                return

            # /prefix username prefix
            elif command == "/prefix" and len(parts) > 2:
                target_name = parts[1]
                new_prefix = parts[2].strip()

                target_user = await sync_to_async(
                    User.objects.filter(username=target_name).first
                )()

                if not target_user:
                    await self.send_json({
                        "error": f"Пользователь {target_name} не найден."
                    })
                    return

                target_profile, _ = await sync_to_async(
                    UserProfile.objects.get_or_create
                )(user=target_user)

                target_profile.prefix = new_prefix

                await sync_to_async(
                    target_profile.save
                )()

                await self.send_system_message(
                    f'Префикс "{new_prefix}" установлен для {target_name}.'
                )
                return

        # Проверка mute
        is_muted = await sync_to_async(
            profile.check_mute_status
        )()

        if is_muted:
            await self.send_json({
                "error": "Вы временно замучены."
            })
            return

        prefix = f"[{profile.prefix}] " if profile.prefix else ""

        full_message = f"{prefix}{user.username}: {message_text}"

        # Сохраняем сообщение
        await sync_to_async(
            Message.objects.create
        )(
            user=user,
            content=message_text
        )

        # Отправляем всем подключённым пользователям
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "message": full_message,
                "username": user.username,
                "prefix": profile.prefix or ""
            }
        )

    async def chat_message(self, event):
        await self.send_json({
            "type": "message",
            "message": event["message"],
            "username": event.get("username", ""),
            "prefix": event.get("prefix", "")
        })

    async def chat_clear(self, event):
        await self.send_json({
            "type": "clear"
        })

    async def send_system_message(self, text):
        # Системное сообщение тоже отправляем всей группе
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "system_message",
                "message": f"[Система]: {text}"
            }
        )

    async def system_message(self, event):
        await self.send_json({
            "type": "system",
            "message": event["message"]
        })