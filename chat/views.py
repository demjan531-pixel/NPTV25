from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import Message


@login_required
def chat_page(request):
    recent_messages = list(
        messages = Message.objects.select_related("user").order_by("-timestamp")[:100]
    )
    recent_messages.reverse()

    messages = [
        {
            "id": message.id,
            "username": message.user.username,
            "text": message.text,
            "created_at": message.created_at.strftime("%H:%M"),
            "is_me": message.user_id == request.user.id,
            "is_admin": bool(message.user.is_staff or message.user.is_superuser),
        }
        for message in recent_messages
    ]
    return render(
        request,
        "chat/chat.html",
        {
            "messages": messages,
            "is_admin": bool(request.user.is_staff or request.user.is_superuser),
        },
    )
