"""Thin adapter for lifecycle rules exposed to the rest of the service layer."""

from app.domain.lifecycle.service import LifecycleDomainService

lifecycle_domain_service = LifecycleDomainService()


def validate_transition(current_status, next_status):
    return lifecycle_domain_service.validate_transition(current_status, next_status)


def validate_cancel_allowed(order: dict):
    lifecycle_domain_service.validate_cancel_allowed(order)


def validate_refund_policy(order: dict):
    return lifecycle_domain_service.validate_refund_policy(order)


__all__ = [
    "lifecycle_domain_service",
    "validate_transition",
    "validate_cancel_allowed",
    "validate_refund_policy",
]
