"""Environment-specific configuration loader for development, local hub, and production."""

import os

from .base import BaseConfig, DevelopmentConfig, LocalHubConfig, ProductionConfig


def resolve_config_class():
    env = (os.getenv("APP_ENV") or os.getenv("SYNC_ROLE") or "development").strip().lower().replace("-", "_")
    if env in {"production", "hub"}:
        return ProductionConfig
    if env in {"local_hub", "node"}:
        return LocalHubConfig
    return DevelopmentConfig


__all__ = [
    "BaseConfig",
    "DevelopmentConfig",
    "LocalHubConfig",
    "ProductionConfig",
    "resolve_config_class",
]
