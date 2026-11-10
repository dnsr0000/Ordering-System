"""Production environment defaults used by dockerized or hosted hub deployments."""

from .base import BaseConfig


class ProductionConfig(BaseConfig):
    ENVIRONMENT = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    TRUSTED_PROXY_COUNT = 1
    SYNC_ROLE = "hub"


__all__ = ["ProductionConfig"]
