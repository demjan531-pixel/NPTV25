from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    prefix = models.CharField(max_length=50, blank=True, null=True, default='')
    is_muted = models.BooleanField(default=False)
    muted_until = models.DateTimeField(null=True, blank=True)
    is_banned = models.BooleanField(default=False)

    def check_mute_status(self):
        if self.is_muted and self.muted_until:
            if timezone.now() > self.muted_until:
                self.is_muted = False
                self.muted_until = None
                self.save()
        return self.is_muted

class Message(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)