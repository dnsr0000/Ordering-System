"""Local hub environment defaults used for localhost testing without a real domain."""

from .base import BaseConfig


class LocalHubConfig(BaseConfig):
    ENVIRONMENT = "local_hub"
    DEBUG = True
    SESSION_COOKIE_SECURE = True
    SYNC_ROLE = "node"
    SYNC_HUB_URL = "https://localhost"


__all__ = ["LocalHubConfig"]
