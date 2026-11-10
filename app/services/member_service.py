"""Service-layer adapter for member reward and coupon business rules."""

from app.domain.member.service import MemberDomainService

member_domain_service = MemberDomainService()


def calculate_points_earned(order_total: float, rate_divisor: int = 10, multiplier: float = 1.0) -> int:
    """Compute earned points using the member domain rules."""
    return member_domain_service.calculate_points_earned(order_total, rate_divisor, multiplier)


def validate_points_deduction(current_points: int, points_to_use: int) -> None:
    """Validate whether a points redemption is allowed."""
    member_domain_service.validate_points_deduction(current_points, points_to_use)


def validate_coupon_code(coupon_code: str) -> str:
    """Normalize and validate a coupon code using the domain contract."""
    return member_domain_service.validate_coupon_code(coupon_code)


def calculate_discount(subtotal: float, rate: float = 0.0) -> float:
    """Compute the discount amount from a subtotal and a discount rate."""
    return member_domain_service.calculate_discount(subtotal, rate)


__all__ = [
    "member_domain_service",
    "calculate_points_earned",
    "validate_points_deduction",
    "validate_coupon_code",
    "calculate_discount",
]
