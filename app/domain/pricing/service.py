"""Business rules for pricing, discount, and service-fee calculations."""

from __future__ import annotations

from app.core.errors import ValidationError


class PricingDomainService:
    """Domain-level pricing rules used by checkout and promotion flows."""

    @staticmethod
    def validate_percentage(value: float, *, label: str = "折扣率", maximum: float = 100.0) -> float:
        numeric = float(value or 0)
        if numeric < 0:
            raise ValidationError(f"{label}不能為負數")
        if numeric > maximum:
            raise ValidationError(f"{label}不能超過 {maximum}")
        return numeric

    @staticmethod
    def calculate_service_charge(subtotal: float, percentage: float) -> int:
        subtotal_value = float(subtotal or 0)
        rate = PricingDomainService.validate_percentage(percentage, label="服務費百分比")
        if subtotal_value <= 0:
            return 0
        return int(round(subtotal_value * rate / 100))

    @staticmethod
    def calculate_discount(subtotal: float, rate: float = 0.0) -> float:
        subtotal_value = float(subtotal or 0)
        if subtotal_value <= 0:
            return 0.0
        safe_rate = max(0.0, min(float(rate or 0), 1.0))
        return round(subtotal_value * safe_rate, 2)

    @staticmethod
    def validate_checkout_total(subtotal: float, discount: float, *, service_charge: float = 0, delivery_fee: float = 0) -> float:
        subtotal_value = float(subtotal or 0)
        discount_value = float(discount or 0)
        service_fee = float(service_charge or 0)
        delivery = float(delivery_fee or 0)
        if discount_value < 0 or service_fee < 0 or delivery < 0:
            raise ValidationError("結帳金額不得為負數")
        total = subtotal_value - discount_value + service_fee + delivery
        if total < 0:
            raise ValidationError("結帳總金額不得為負數")
        return round(total, 2)


__all__ = ["PricingDomainService"]
