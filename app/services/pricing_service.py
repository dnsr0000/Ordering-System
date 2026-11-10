"""Thin adapter exposing pricing rules from the domain layer."""

from app.domain.pricing.service import PricingDomainService

pricing_domain_service = PricingDomainService()


def calculate_service_charge(subtotal: float, percentage: float) -> int:
    return pricing_domain_service.calculate_service_charge(subtotal, percentage)


def calculate_discount(subtotal: float, rate: float = 0.0) -> float:
    return pricing_domain_service.calculate_discount(subtotal, rate)


def validate_checkout_total(subtotal: float, discount: float, *, service_charge: float = 0, delivery_fee: float = 0) -> float:
    return pricing_domain_service.validate_checkout_total(
        subtotal,
        discount,
        service_charge=service_charge,
        delivery_fee=delivery_fee,
    )


__all__ = [
    "pricing_domain_service",
    "calculate_service_charge",
    "calculate_discount",
    "validate_checkout_total",
]
