import json
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.models import User
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from .models import Message, UserProfile

class ChatConsumer(AsyncWebsocketConsumer):
    async def receive(self, text_data):
        data = json.loads(text_data)
        message_text = data.get('message', '').strip()
        user = self.scope['user']

        if not user.is_authenticated:
            return

        # Проверка на бан
        profile, _ = await sync_to_async(UserProfile.objects.get_or_create)(user=user)
        if profile.is_banned:
            await self.send(text_data=json.dumps({'error': 'Вы забанены.'}))
            return

        # Обработка команд администратора
        if message_text.startswith('/'):
            if not user.is_superuser:
                await self.send(text_data=json.dumps({'error': 'У вас нет прав для выполнения этой команды.'}))
                return

            parts = message_text.split(' ', 2)
            command = parts[0].lower()

            # Команда /clear — очистка всех сообщений
            if command == '/clear':
                await sync_to_async(Message.objects.all().delete)()
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {'type': 'chat_clear'}
                )
                return

            # Команда /ban <username>
            elif command == '/ban' and len(parts) > 1:
                target_name = parts[1]
                target_user = await sync_to_async(User.objects.filter(username=target_name).first)()
                if target_user:
                    target_profile, _ = await sync_to_async(UserProfile.objects.get_or_create)(user=target_user)
                    target_profile.is_banned = True
                    await sync_to_async(target_profile.save)()
                    await self.send_system_message(f'Пользователь {target_name} забанен.')
                return

            # Команда /mute <username> <минуты>
            elif command == '/mute' and len(parts) > 2:
                target_name = parts[1]
                try:
                    minutes = int(parts[2])
                except ValueError:
                    return

                target_user = await sync_to_async(User.objects.filter(username=target_name).first)()
                if target_user:
                    target_profile, _ = await sync_to_async(UserProfile.objects.get_or_create)(user=target_user)
                    target_profile.is_muted = True
                    target_profile.muted_until = timezone.now() + timedelta(minutes=minutes)
                    await sync_to_async(target_profile.save)()
                    await self.send_system_message(f'Пользователь {target_name} замучен на {minutes} минут.')
                return

            # Команда /prefix <username> <префикс>
            elif command == '/prefix' and len(parts) > 2:
                target_name = parts[1]
                new_prefix = parts[2]
                target_user = await sync_to_async(User.objects.filter(username=target_name).first)()
                if target_user:
                    target_profile, _ = await sync_to_async(UserProfile.objects.get_or_create)(user=target_user)
                    target_profile.prefix = new_prefix
                    await sync_to_async(target_profile.save)()
                    await self.send_system_message(f'Префикс "{new_prefix}" установлен для {target_name}.')
                return

        # Проверка мута перед отправкой обычного сообщения
        is_muted = await sync_to_async(profile.check_mute_status)()
        if is_muted:
            await self.send(text_data=json.dumps({'error': 'Вы временно замучены.'}))
            return

        # Отправка обычного сообщения с учетом префикса
        prefix = f"[{profile.prefix}] " if profile.prefix else ""
        full_message = f"{prefix}{user.username}: {message_text}"

        await sync_to_async(Message.objects.create)(user=user, content=message_text)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': full_message,
                'username': user.username,
                'prefix': profile.prefix
            }
        )

    async def chat_clear(self, event):
        await self.send(text_data=json.dumps({'type': 'clear'}))

    async def send_system_message(self, text):
        await self.send(text_data=json.dumps({'message': f'[Система]: {text}'}))