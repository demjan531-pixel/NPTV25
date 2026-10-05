from django.contrib import admin
from .models import Message, NewsMessage, UserProfile


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('user', 'content', 'timestamp')
    list_filter = ('timestamp', 'user')
    search_fields = ('content', 'user__username')


@admin.register(NewsMessage)
class NewsMessageAdmin(admin.ModelAdmin):
    list_display = ('user', 'content', 'timestamp')
    list_filter = ('timestamp', 'user')
    search_fields = ('content', 'user__username')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'prefix', 'is_muted', 'muted_until', 'is_banned')
    list_filter = ('is_banned', 'is_muted')
    search_fields = ('user__username', 'prefix')
