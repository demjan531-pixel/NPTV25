"""
URL configuration for myproject project.

The `urlpatterns` list routes URLs to views. For more information please see:
https://docs.djangoproject.com/en/6.1/topics/http/urls/
"""
from django.contrib import admin
from django.urls import include, path
from django.contrib.auth import views as auth_views
from main import views
from chat import views as chat_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('register/', views.register, name='register'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('terms/', views.terms, name='terms'),
    path('chat/', include('chat.urls')),
    path('news/', chat_views.news_page, name='news'),
]
