"""Order domain service for state transitions and validation."""

from __future__ import annotations

from typing import Any

from app.core.errors import ValidationError


class OrderDomainService:
    """Business rules for order creation and lifecycle transitions."""

    VALID_STATUSES = {"pending", "paid", "preparing", "ready", "completed", "cancelled"}
    VALID_PAYMENT_METHODS = {"cash", "credit_card", "stored_value", "gift_card", "line_pay", "jko_pay", "apple_pay", "split", "platform"}
    VALID_PAYMENT_STATUSES = {"", "pending", "authorizing", "paid", "settled", "unpaid", "expired", "refund_required", "refunded", "cancelled"}
    LEGAL_TRANSITIONS = {
        "pending": {"paid", "cancelled"},
        "paid": {"preparing", "ready", "cancelled"},
        "preparing": {"ready", "cancelled"},
        "ready": {"completed", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    }

    @staticmethod
    def validate_payment_method(method: Any) -> str:
        value = str(method or "").strip().lower()
        if value not in OrderDomainService.VALID_PAYMENT_METHODS:
            raise ValidationError(f"不支援的付款方式: {method}")
        return value

    @staticmethod
    def validate_payment_status(status: Any) -> str:
        value = str(status or "").strip().lower()
        if value not in OrderDomainService.VALID_PAYMENT_STATUSES:
            raise ValidationError(f"不支援的付款狀態: {status}")
        return value

    @staticmethod
    def validate_payload(payload: dict[str, Any], tenant_id: int) -> dict[str, Any]:
        if not tenant_id:
            raise ValidationError("租戶識別碼不得為空")
        items = payload.get("items", [])
        if not items:
            raise ValidationError("訂單至少需要一筆品項")
        normalized_items: list[dict[str, Any]] = []
        total = 0.0
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValidationError(f"第 {idx} 筆品項格式錯誤")
            quantity = int(item.get("quantity", 1))
            if quantity <= 0:
                raise ValidationError(f"第 {idx} 筆品項數量必須大於 0")
            price = float(item.get("price", 0.0))
            if price < 0:
                raise ValidationError(f"第 {idx} 筆品項金額不能為負數")
            normalized_item = dict(item)
            normalized_item["quantity"] = quantity
            normalized_item["price"] = round(price, 2)
            normalized_items.append(normalized_item)
            total += normalized_item["price"] * quantity
        return {
            "tenant_id": tenant_id,
            "items": normalized_items,
            "status": "pending",
            "total": round(total, 2),
        }

    @staticmethod
    def create_order(payload: dict[str, Any], tenant_id: int) -> dict[str, Any]:
        return OrderDomainService.validate_payload(payload, tenant_id)

    @classmethod
    def validate_transition(cls, current_status: str, next_status: str) -> str:
        current = str(current_status or "pending")
        target = str(next_status or "").strip()
        if target not in cls.VALID_STATUSES:
            raise ValidationError(f"不支援的訂單狀態: {next_status}")
        if target not in cls.LEGAL_TRANSITIONS.get(current, set()):
            raise ValidationError(f"訂單狀態不能從 {current} 直接轉為 {target}")
        return target

    @classmethod
    def transition_status(cls, order: dict[str, Any], next_status: str) -> dict[str, Any]:
        current_status = str(order.get("status", "pending"))
        order["status"] = cls.validate_transition(current_status, next_status)
        return order

    @staticmethod
    def validate_cancel_policy(order: dict[str, Any]) -> bool:
        status = str(order.get("status") or "").strip().lower()
        if status == "completed":
            raise ValidationError("已完成訂單不可取消")
        if status == "cancelled":
            raise ValidationError("此訂單已經取消")
        if (order.get("is_paid") or False) and float(order.get("total_price") or 0) > 0:
            return True
        return True

    @staticmethod
    def validate_cancel_allowed(order: dict[str, Any]) -> None:
        OrderDomainService.validate_cancel_policy(order)

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

    @staticmethod
    def validate_checkout_total(subtotal: float, discount: float, *, service_charge: float = 0, delivery_fee: float = 0) -> float:
        subtotal = float(subtotal or 0)
        discount = float(discount or 0)
        service_charge = float(service_charge or 0)
        delivery_fee = float(delivery_fee or 0)
        if discount < 0 or service_charge < 0 or delivery_fee < 0:
            raise ValidationError("結帳金額不得為負數")
        total = subtotal - discount + service_charge + delivery_fee
        if total < 0:
            raise ValidationError("結帳總金額不得為負數")
        return round(total, 2)

    @staticmethod
    def validate_expected_total(subtotal: float, discount: float, *, service_charge: float = 0, delivery_fee: float = 0,
                               expected_total: Any = None) -> float:
        total = OrderDomainService.validate_checkout_total(
            subtotal,
            discount,
            service_charge=service_charge,
            delivery_fee=delivery_fee,
        )
        if expected_total in (None, ""):
            return total
        parsed = float(expected_total)
        if parsed != total:
            raise ValidationError(f"結帳金額已變動：畫面顯示 ${expected_total}，目前應付 ${total}")
        return total

    @staticmethod
    def calculate_total(items: list[dict[str, Any]]) -> float:
        return round(sum(float(item.get("price", 0.0)) * int(item.get("quantity", 1)) for item in items), 2)


__all__ = ["OrderDomainService"]
