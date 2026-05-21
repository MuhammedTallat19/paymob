import os

from celery import Celery


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "policy_management.settings")

app = Celery("policy_management")

# Read CELERY_* settings from Django settings.py
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks from installed apps
app.autodiscover_tasks()

