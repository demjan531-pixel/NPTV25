from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils.timezone import localtime

from .models import Message


@login_required
def chat_page(request):
    recent_messages = (
        Message.objects
        .select_related("user")
        .order_by("-timestamp")[:100]
    )

    messages = [
        {
            "id": message.id,
            "username": message.user.username,
            "is_admin": message.user.is_staff or message.user.is_superuser,
            "is_me": message.user_id == request.user.id,
            "text": message.content,
            "created_at": localtime(message.timestamp).strftime("%H:%M"),
        }
        for message in reversed(list(recent_messages))
    ]

    return render(
        request,
        "chat/chat.html",
        {
            "messages": messages,
            "is_admin": request.user.is_staff or request.user.is_superuser,
        },
    )
