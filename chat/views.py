from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils.timezone import localtime

from .models import Message, NewsMessage, UserProfile


@login_required
def chat_page(request):
    recent_messages = (
        Message.objects
        .select_related("user")
        .order_by("-timestamp")[:100]
    )

    messages = []

    for message in reversed(list(recent_messages)):
        profile, _ = UserProfile.objects.get_or_create(user=message.user)

        messages.append({
            "id": message.id,
            "username": message.user.username,
            "prefix": profile.prefix or "",
            "prefix_color": profile.prefix_color or "",
            "is_admin": (
                message.user.is_staff or
                message.user.is_superuser
            ),
            "is_me": message.user_id == request.user.id,
            "text": message.content,
            "created_at": localtime(message.timestamp).strftime("%H:%M"),
        })

    current_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    return render(
        request,
        "chat/chat.html",
        {
            "messages": messages,
            "is_admin": (
                request.user.is_staff or
                request.user.is_superuser
            ),
            "current_prefix": current_profile.prefix or "",
        },
    )


@login_required
def news_page(request):
    recent_news = (
        NewsMessage.objects
        .select_related("user")
        .order_by("-timestamp")[:100]
    )

    news = []

    for item in reversed(list(recent_news)):
        profile, _ = UserProfile.objects.get_or_create(user=item.user)

        news.append({
            "id": item.id,
            "username": item.user.username,
            "prefix": profile.prefix or "",
            "prefix_color": profile.prefix_color or "",
            "is_admin": (
                item.user.is_staff or
                item.user.is_superuser
            ),
            "text": item.content,
            "created_at": localtime(item.timestamp).strftime("%d.%m.%Y %H:%M"),
        })

    return render(
        request,
        "chat/news.html",
        {
            "news": news,
            "is_admin": (
                request.user.is_staff or
                request.user.is_superuser
            ),
        },
    )
