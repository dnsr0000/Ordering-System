"""Member domain service for points, coupons and rewards logic."""

from __future__ import annotations

from typing import Any
from app.core.errors import ValidationError


class MemberDomainService:
    """Business rules for member reward and coupon operations."""

    @staticmethod
    def calculate_points_earned(order_total: float, rate_divisor: int = 10, multiplier: float = 1.0) -> int:
        """
        業務規則 1：計算訂單產生的點數。
        支援基礎兌換比例（預設 10 元 1 點）與會員等級倍率（multiplier）。
        """
        if order_total <= 0 or rate_divisor <= 0:
            return 0
        base_points = int(order_total // rate_divisor)
        safe_multiplier = max(0.0, multiplier)
        return int(base_points * safe_multiplier)

    @staticmethod
    def validate_points_deduction(current_points: int, points_to_use: int) -> None:
        """
        業務規則 2：驗證欲折抵的點數是否合法。
        """
        if points_to_use < 0:
            raise ValidationError("折抵點數不能為負數")
        if points_to_use > current_points:
            raise ValidationError("會員點數不足，無法折抵")

    @staticmethod
    def validate_coupon_code(coupon_code: str) -> str:
        """
        業務規則 3：檢查優惠券代碼格式。
        去空格並轉大寫，防呆避免注入空白字元。
        """
        cleaned = (coupon_code or "").strip().upper()
        if not cleaned:
            raise ValidationError("優惠券代碼不能為空")
        return cleaned

    @staticmethod
    def calculate_discount(subtotal: float, rate: float = 0.0) -> float:
        """
        業務規則 4：計算折扣折抵金額。
        折扣率限制在 0.0 ~ 1.0 之間。
        """
        if subtotal <= 0:
            return 0.0
        safe_rate = max(0.0, min(rate, 1.0))
        return round(subtotal * safe_rate, 2)


__all__ = ["MemberDomainService"]