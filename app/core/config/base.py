"""Base application configuration for environment-aware service settings."""

import os

from app.config import Config as LegacyConfig


class BaseConfig(LegacyConfig):
    """Thin compatibility layer over the legacy app config.

    This keeps the larger runtime settings in one place while exposing the new
    environment-aware config classes the refactor is moving toward.
    """

    ENVIRONMENT = os.getenv("APP_ENV", "development")
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    TESTING = False

    @classmethod
    def from_env(cls):
        """Return configuration object matching the active environment."""
        env = (os.getenv("APP_ENV") or "development").lower().replace("-", "_")
        if env == "production":
            return ProductionConfig()
        if env == "local_hub":
            return LocalHubConfig()
        return DevelopmentConfig()


class DevelopmentConfig(BaseConfig):
    ENVIRONMENT = "development"
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    SYNC_ROLE = "standalone"
    SYNC_HUB_URL = ""
    SYNC_NODE_TOKEN = ""


class LocalHubConfig(BaseConfig):
    ENVIRONMENT = "local_hub"
    DEBUG = True
    SESSION_COOKIE_SECURE = True
    SYNC_ROLE = "node"
    SYNC_HUB_URL = os.getenv("SYNC_HUB_URL", "https://localhost")


class ProductionConfig(BaseConfig):
    ENVIRONMENT = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    TRUSTED_PROXY_COUNT = 1
    SYNC_ROLE = "hub"


__all__ = [
    "BaseConfig",
    "DevelopmentConfig",
    "LocalHubConfig",
    "ProductionConfig",
]
