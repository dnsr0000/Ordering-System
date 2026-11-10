"""Shared domain errors used by service-layer validation and workflow orchestration."""


class DomainError(Exception):
    """Base class for all application-level domain errors."""


class ValidationError(DomainError):
    """Raised when input data fails business validation."""


class TenantNotFoundError(DomainError):
    """Raised when a tenant or shop context cannot be resolved."""


class SyncConflictError(DomainError):
    """Raised when local and remote state conflict during sync."""


class PaymentError(DomainError):
    """Raised when payment state or provider response is invalid."""


class RetryableSyncError(DomainError):
    """Raised when a sync operation can be retried safely."""


__all__ = [
    "DomainError",
    "ValidationError",
    "TenantNotFoundError",
    "SyncConflictError",
    "PaymentError",
    "RetryableSyncError",
]
