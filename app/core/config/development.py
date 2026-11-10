"""Development environment defaults used by local single-store runs."""

from .base import BaseConfig


class DevelopmentConfig(BaseConfig):
    ENVIRONMENT = "development"
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    SYNC_ROLE = "standalone"
    SYNC_HUB_URL = ""
    SYNC_NODE_TOKEN = ""


__all__ = ["DevelopmentConfig"]
