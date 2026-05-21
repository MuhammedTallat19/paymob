"""
Project package init.

Celery is optional in some environments; the app should still boot without it.
"""

try:
    from .celery import app as celery_app  # type: ignore

    __all__ = ("celery_app",)
except Exception:
    # Celery not installed / not configured yet.
    celery_app = None  # type: ignore
    __all__ = ()
