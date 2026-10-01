from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import Message


@login_required
def chat_page(request):
    recent_messages = list(
        Message.objects
        .select_related("user")
        .order_by("-timestamp")[:100]
    )

    recent_messages.reverse()

    return render(
        request,
        "chat/chat.html",
        {
            "messages": recent_messages,
        }
    )