"""Domain service for menu management and catalog operations."""

from __future__ import annotations

import re
from typing import Any

from app.core.errors import ValidationError


class MenuDomainService:
    """Business rules and validations for menu entities."""

    NON_MAIN_ADDON_CATEGORIES = ("加購", "附餐", "飲料", "加料", "加點")
    MAX_AMOUNT = 1_000_000
    NAME_MAX = 100
    CATEGORY_MAX = 50
    DESCRIPTION_MAX = 200

    @staticmethod
    def validate_category(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("分類名稱不能為空")
        if len(name) > 50:
            raise ValidationError("分類名稱不能超過 50 個字元")
        return {
            "name": name,
            "display_order": int(payload.get("display_order", 0)),
            "is_active": bool(payload.get("is_active", True)),
        }

    @staticmethod
    def validate_item(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("品項名稱不能為空")
        try:
            price = float(payload.get("price", 0.0))
        except (ValueError, TypeError) as exc:
            raise ValidationError("品項價格格式錯誤") from exc
        if price < 0:
            raise ValidationError("品項價格不能為負數")
        if price > MenuDomainService.MAX_AMOUNT:
            raise ValidationError(f"品項價格不可超過 {MenuDomainService.MAX_AMOUNT}")
        return {
            "name": name,
            "price": round(price, 2),
            "category_id": payload.get("category_id"),
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    def validate_item_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """供 menu_service.apply_menu_form 驗證名稱與價格。"""
        return cls.validate_item(payload)

    @classmethod
    def validate_item_submission(cls, name: Any, price: Any, *, category: Any = "主餐", description: Any = "",
                                 can_be_add_on: bool = False, add_on_price: Any = 0,
                                 discount_price: Any = 0) -> dict[str, Any]:
        """Normalize and validate the item fields consumed by menu_service.apply_menu_form."""
        name = cls.validate_length(str(name or "").strip(), cls.NAME_MAX, "餐點名稱")
        category = cls.validate_length(str(category or "主餐").strip(), cls.CATEGORY_MAX, "分類")
        description = cls.validate_length(str(description or "").strip(), cls.DESCRIPTION_MAX, "商品描述")
        validated_price = cls.validate_item({"name": name, "price": price})["price"]
        addon_price = cls._number(add_on_price, "加購專屬價")
        discount = cls._number(discount_price, "特價金額")
        cls.validate_range(addon_price, cls.MAX_AMOUNT, "加購專屬價")
        cls.validate_range(discount, cls.MAX_AMOUNT, "特價金額")
        if can_be_add_on and addon_price > validated_price:
            raise ValidationError("加購專屬價不可高於原價！")
        return {
            "name": name,
            "price": validated_price,
            "category": category,
            "description": description,
            "can_be_add_on": bool(can_be_add_on),
            "add_on_price": addon_price,
            "discount_price": discount,
        }

    @staticmethod
    def _number(value: Any, label: str) -> float:
        try:
            return float(value or 0)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{label}格式錯誤") from exc

    @staticmethod
    def validate_length(value: Any, limit: int, label: str) -> str:
        text = str(value or "")
        if len(text) > limit:
            raise ValidationError(f"{label}不可超過 {limit} 個字元")
        return text

    @staticmethod
    def validate_range(value: Any, limit: int, label: str) -> Any:
        if value < 0:
            raise ValidationError(f"{label}不可為負數")
        if value > limit:
            raise ValidationError(f"{label}不可超過 {limit}")
        return value

    @classmethod
    def validate_reward_status(cls, price: Any, is_reward: bool, reward_points: Any,
                               reward_discount_points: Any = 0) -> dict[str, Any]:
        points = cls._number(reward_points, "兌換點數")
        discount_points = cls._number(reward_discount_points, "特惠點數")
        cls.validate_range(points, cls.MAX_AMOUNT, "兌換點數")
        cls.validate_range(discount_points, cls.MAX_AMOUNT, "特惠點數")
        if not is_reward:
            return {"is_reward": False, "reward_points": 0, "reward_discount_points": 0}
        item_price = cls._number(price, "品項價格")
        if points <= 0 or (item_price > 0 and points > item_price):
            raise ValidationError("兌換點數必須大於 0，且不得高於原單價")
        if discount_points < 0 or discount_points >= points:
            raise ValidationError("特惠點數必須大於等於 0 且小於兌換點數")
        return {"is_reward": True, "reward_points": int(points), "reward_discount_points": int(discount_points)}

    @classmethod
    def validate_combo_submission(cls, name: Any, *, main_id: Any, additional_price: Any = 0,
                                  description: Any = "") -> dict[str, Any]:
        normalized_name = cls.validate_length(str(name or "").strip(), cls.NAME_MAX, "套餐名稱")
        if not normalized_name:
            raise ValidationError("套餐名稱不能為空")
        try:
            normalized_main_id = int(main_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError("請選擇主餐") from exc
        if normalized_main_id <= 0:
            raise ValidationError("請選擇主餐")
        price = cls._number(additional_price, "套餐加價")
        cls.validate_range(price, cls.MAX_AMOUNT, "套餐加價")
        normalized_description = cls.validate_length(str(description or "").strip(), cls.DESCRIPTION_MAX, "套餐描述")
        return {
            "name": normalized_name,
            "main_item_id": normalized_main_id,
            "additional_price": price,
            "description": normalized_description,
        }

    @classmethod
    def validate_import_number(cls, value: Any, label: str, item_name: str, limit: int,
                              to_int=round) -> Any:
        if value is None or value == "":
            return 0
        cleaned = re.sub(r"(?i)ntd?|[$元,\s]", "", str(value)).strip()
        try:
            number = float(cleaned)
            if to_int is int and not number.is_integer():
                raise ValueError
            normalized = to_int(number)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValidationError(f"餐點「{item_name}」的{label}格式錯誤") from exc
        if normalized < 0:
            raise ValidationError(f"餐點「{item_name}」的{label}不可為負數")
        if normalized > limit:
            raise ValidationError(f"餐點「{item_name}」的{label}不可超過 {limit}")
        return normalized

    @staticmethod
    def validate_addon_quota(main_units: int, addon_units: int) -> str | None:
        """
        加購配額規則：1 份主餐最多加購 2 份加購品。
        超額時回傳錯誤訊息字串，未超額回傳 None。
        """
        quota = main_units * 2
        if addon_units > quota:
            return f"加購品數量 ({addon_units}) 超出配額 (主餐 {main_units} 份最多加購 {quota} 份)"
        return None


__all__ = ["MenuDomainService"]