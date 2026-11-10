"""Business rules for order lifecycle transitions and cancellation/refund decisions."""

from __future__ import annotations

from typing import Any

from app.core.errors import ValidationError


class LifecycleDomainService:
    """Business rules for order lifecycle and refund decisions."""

    VALID_STATUSES = {
        "pending",
        "preparing",
        "ready",
        "served",
        "completed",
        "picked_up",
        "cancelled",
    }
    LEGAL_TRANSITIONS = {
        "pending": {"preparing", "completed", "cancelled"},
        "preparing": {"ready", "completed", "cancelled"},
        "ready": {"served", "completed", "picked_up", "cancelled"},
        "completed": {"picked_up", "cancelled"},
        "picked_up": {"cancelled"},
        "served": {"completed", "cancelled"},
        "cancelled": set(),
    }

    @classmethod
    def normalize_status(cls, value: Any) -> str:
        raw = str(getattr(value, "value", value) or "").strip()
        if not raw:
            return "pending"

        cleaned = raw.lower().replace("-", "_").replace(" ", "")

        alias_map = {
            "pickedup": "picked_up",
            "completed": "completed",
            "cancelled": "cancelled",
            "pending": "pending",
            "ready": "ready",
            "preparing": "preparing",
            "served": "served",
        }
        normalized = alias_map.get(cleaned, cleaned)

        if normalized not in cls.VALID_STATUSES:
            raise ValidationError(f"不支援的訂單狀態: {value}")
        return normalized

    @classmethod
    def validate_transition(cls, current_status: Any, next_status: Any) -> str:
        current = cls.normalize_status(current_status)
        target = cls.normalize_status(next_status)
        if target not in cls.LEGAL_TRANSITIONS.get(current, set()):
            raise ValidationError(f"訂單狀態不能從 {current} 直接轉為 {target}")
        return target

    @staticmethod
    def validate_cancel_allowed(order: dict[str, Any]) -> None:
        status = str(order.get("status") or "").strip().lower()
        if status == "completed":
            raise ValidationError("已完成訂單不可取消")
        if status == "cancelled":
            raise ValidationError("此訂單已經取消")
        if (order.get("is_paid") or False) and float(order.get("total_price") or 0) > 0:
            return
        if (order.get("payment_status") or "").strip().lower() in {"pending", "authorizing", "expired", "unpaid"}:
            return
        return

    @staticmethod
    def validate_refund_policy(order: dict[str, Any]) -> float:
        payment_status = str(order.get("payment_status") or "").strip().lower()
        refundable = float(order.get("refundable_amount") or 0)
        if payment_status in {"pending", "authorizing", "expired", "unpaid"}:
            raise ValidationError("此訂單尚未完成付款，不能退款")
        if refundable < 0:
            raise ValidationError("退款金額不能為負數")
        if payment_status in {"refund_required", "refunded"} and refundable <= 0:
            return 0.0
        return round(refundable, 2)


__all__ = ["LifecycleDomainService"]