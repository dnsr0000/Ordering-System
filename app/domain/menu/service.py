"""Menu domain service as the first step of the system-domain split."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from app.core.errors import ValidationError

MAX_AMOUNT = 1_000_000
MAX_STOCK = 100_000
NAME_MAX = 100
CATEGORY_MAX = 50
DESCRIPTION_MAX = 200
LOW_STOCK = 5
_NUMBER_NOISE_RE = re.compile(r'(?i)ntd?|[$元,\s]')


class MenuDomainService:
    """Encapsulates menu, stock and menu-item business rules.

    This service is deliberately independent from Flask routes so it can later be
    reused by both local storefront and hub-synced nodes.
    """

    NON_MAIN_ADDON_CATEGORIES = ("飲料", "點心", "副餐", "配餐")

    @staticmethod
    def normalize_category(value: Any) -> str:
        return (str(value or "")).strip().lower()

    @classmethod
    def is_main_category(cls, category: Any) -> bool:
        cat = cls.normalize_category(category)
        if not cat:
            return True
        return cat not in {cls.normalize_category(name) for name in cls.NON_MAIN_ADDON_CATEGORIES}

    @classmethod
    def addon_qualifies(cls, main_item: Any, addon_item: Any) -> bool:
        """Return whether a main dish qualifies for the given add-on item."""
        trigger = (getattr(addon_item, "addon_trigger_type", None) or "any").strip().lower()
        target = (getattr(addon_item, "addon_trigger_target", "") or "").strip().lower()
        main_cat = (getattr(main_item, "category", "") or "未分類").strip()
        if trigger == "category":
            return cls.normalize_category(main_cat) == (target or cls.normalize_category(getattr(addon_item, "category", "")))
        if trigger == "item":
            return str(getattr(main_item, "id", "")) == target or cls.normalize_category(getattr(main_item, "name", "")) == target
        return cls.is_main_category(main_cat)

    @staticmethod
    def validate_length(value: Any, limit: int, label: str = "文字") -> Any:
        if len(value or "") > limit:
            raise ValidationError(f"{label}最多 {limit} 個字")
        return value

    @staticmethod
    def validate_range(value: Any, limit: int, label: str = "數值") -> Any:
        if not 0 <= value <= limit:
            raise ValidationError(f"{label}必須介於 0 ~ {limit:,}！(目前為 {value:,})")
        return value

    @staticmethod
    def validate_name(name: str, label: str = "餐點名稱") -> str:
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValidationError(f"{label}不能為空")
        if len(cleaned) > NAME_MAX:
            raise ValidationError(f"{label}最多 {NAME_MAX} 個字")
        return cleaned

    @staticmethod
    def validate_combo_name(name: str, label: str = "套餐名稱") -> str:
        return MenuDomainService.validate_name(name, label)

    @staticmethod
    def validate_price(value: Any, label: str = "單價") -> float:
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label}必須是數字") from exc
        if amount < 0 or amount > MAX_AMOUNT:
            raise ValidationError(f"{label}必須介於 0 ~ {MAX_AMOUNT:,} 之間")
        return round(amount, 2)

    @staticmethod
    def validate_import_number(value: Any, label: str, item_name: str, limit: int, *, to_int=round):
        if value is None or value == "":
            return 0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num = float(value)
        else:
            cleaned = unicodedata.normalize("NFKC", str(value))
            cleaned = _NUMBER_NOISE_RE.sub("", cleaned)
            try:
                num = float(cleaned)
            except ValueError as exc:
                raise ValidationError(f"餐點「{item_name}」的{label}「{value}」無法辨識，請填寫 0 以上的數字") from exc
        if num < 0:
            raise ValidationError(f"餐點「{item_name}」的{label}「{value}」無法辨識，請填寫 0 以上的數字")
        num = to_int(num)
        if num > limit:
            raise ValidationError(f"餐點「{item_name}」的{label}必須介於 0 ~ {limit:,}！(目前為 {num:,})")
        return num

    @staticmethod
    def validate_item_submission(name: Any, price: Any, *, category: Any = "主餐", description: Any = "", stock: Any = 0,
                                total_stock: Any = 0, can_be_add_on: bool = False, add_on_price: Any = 0,
                                discount_price: Any = 0) -> dict[str, Any]:
        validated_name = MenuDomainService.validate_name(name, "餐點名稱")
        validated_price = MenuDomainService.validate_price(price, "單價")
        cat = (category or "主餐").strip()
        if not cat:
            cat = "主餐"
        MenuDomainService.validate_length(cat, CATEGORY_MAX, "分類")
        MenuDomainService.validate_length(description or "", DESCRIPTION_MAX, "餐點說明")
        MenuDomainService.validate_range(max(0, round(float(validated_price))), MAX_AMOUNT, "單價")
        if can_be_add_on:
            addon_price = MenuDomainService.validate_range(max(0, float(add_on_price or 0)), MAX_AMOUNT, "加購專屬價")
            if addon_price <= 0:
                raise ValidationError("已開啟加購品標籤，請輸入大於 0 的加購專屬價！")
            if addon_price >= validated_price:
                raise ValidationError(f"加購專屬價 (${addon_price}) 不得高於或等於原單價 (${validated_price})！")
        MenuDomainService.validate_range(max(0, int(stock or 0)), MAX_STOCK, "剩餘數量")
        MenuDomainService.validate_range(max(0, int(total_stock or 0)), MAX_STOCK, "總量")
        MenuDomainService.validate_range(max(0, float(discount_price or 0)), MAX_AMOUNT, "特價金額")
        return {
            "name": validated_name,
            "price": validated_price,
            "category": cat,
            "description": description or "",
            "stock": max(0, int(stock or 0)),
            "total_stock": max(0, int(total_stock or 0)),
            "can_be_add_on": bool(can_be_add_on),
            "add_on_price": max(0, float(add_on_price or 0)),
            "discount_price": max(0, float(discount_price or 0)),
        }

    @staticmethod
    def validate_stock(value: Any, label: str = "庫存") -> int:
        try:
            stock = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label}必須是整數") from exc
        if stock < 0 or stock > MAX_STOCK:
            raise ValidationError(f"{label}必須介於 0 ~ {MAX_STOCK:,} 之間")
        return stock

    @staticmethod
    def validate_combo_price(value: Any, label: str = "套餐加價") -> float:
        return MenuDomainService.validate_price(value, label)

    @staticmethod
    def validate_reward_status(item_price: Any, is_reward: bool, reward_points: Any = 0, reward_discount_points: Any = 0) -> dict[str, Any]:
        pts = max(0, int(reward_points or 0))
        disc = max(0, int(reward_discount_points or 0))
        if is_reward:
            if pts <= 0:
                raise ValidationError("啟用紅利兌換時，「兌換所需點數」必須大於 0！")
            if (float(item_price or 0) > 0) and pts > float(item_price or 0):
                raise ValidationError(f"兌換所需點數 ({pts} 點) 不得高於原單價 (${item_price})！")
            if pts > MAX_AMOUNT:
                raise ValidationError(f"兌換所需點數不得超過 {MAX_AMOUNT:,} 點！")
            if disc and disc >= pts:
                raise ValidationError("「限時特惠點數」必須小於「兌換所需點數」！")
        else:
            pts = disc = 0
        return {
            "is_reward": bool(is_reward),
            "reward_points": pts,
            "reward_discount_points": disc,
        }

    @staticmethod
    def validate_combo_submission(name: Any, *, main_id: Any, additional_price: Any = 0, description: Any = "") -> dict[str, Any]:
        combo_name = MenuDomainService.validate_name(name, "套餐名稱")
        MenuDomainService.validate_length(description or "", DESCRIPTION_MAX, "套餐說明")
        if not main_id:
            raise ValidationError("請選擇主餐！")
        price = MenuDomainService.validate_range(max(0, int(additional_price or 0)), MAX_AMOUNT, "套餐加價")
        return {
            "name": combo_name,
            "main_item_id": int(main_id),
            "additional_price": price,
            "description": description or "",
        }

    @staticmethod
    def validate_item_payload(payload: dict[str, Any]) -> dict[str, Any]:
        name = MenuDomainService.validate_name(payload.get("name"), "餐點名稱")
        price = MenuDomainService.validate_price(payload.get("price"), "單價")
        payload["name"] = name
        payload["price"] = price
        return payload

    @staticmethod
    def calculate_total(items: Iterable[dict[str, Any]]) -> float:
        total = 0.0
        for item in items:
            quantity = max(1, int(item.get("quantity", 1)))
            unit_price = MenuDomainService.validate_price(item.get("price", 0), "單價")
            total += quantity * unit_price
        return round(total, 2)

    @staticmethod
    def ensure_stock_available(item: dict[str, Any], ordered_quantity: int) -> None:
        available = MenuDomainService.validate_stock(item.get("stock", 0), "庫存")
        if ordered_quantity > available:
            raise ValidationError(f"庫存不足，最多只能下訂 {available} 份")

    @classmethod
    def validate_addon_quota(cls, main_units: Iterable[Any], addon_units: Iterable[Any]) -> str | None:
        """Validate the max-flow add-on quota using the same rules as checkout."""
        mains, addons = {}, {}
        for item, qty in main_units:
            mains.setdefault(item.id, [item, 0])[1] += qty
        for item, qty in addon_units:
            addons.setdefault(item.id, [item, 0])[1] += qty

        total_addons = sum(q for _, q in addons.values())
        if total_addons == 0:
            return None

        msg = "加購品數量超過可搭配的合格餐點數量，請調整購物車後重新結帳！"
        if total_addons > sum(q for _, q in mains.values()):
            return msg

        src, snk = ("s",), ("t",)
        cap, adj = {}, {}

        def add_edge(u, v, c):
            cap[(u, v)] = cap.get((u, v), 0) + c
            cap.setdefault((v, u), 0)
            adj.setdefault(u, set()).add(v)
            adj.setdefault(v, set()).add(u)

        for aid, (a_item, a_qty) in addons.items():
            add_edge(src, ("a", aid), a_qty)
            for mid, (m_item, _m_qty) in mains.items():
                if cls.addon_qualifies(m_item, a_item):
                    add_edge(("a", aid), ("m", mid), a_qty)
        for mid, (_m_item, m_qty) in mains.items():
            add_edge(("m", mid), snk, m_qty)

        flow = 0
        while True:
            parent, frontier = {src: None}, [src]
            for u in frontier:
                for v in adj.get(u, ()):
                    if v not in parent and cap[(u, v)] > 0:
                        parent[v] = u
                        frontier.append(v)
            if snk not in parent:
                break
            path, v = [], snk
            while parent[v] is not None:
                path.append((parent[v], v))
                v = parent[v]
            bottleneck = min(cap[e] for e in path)
            for u, v in path:
                cap[(u, v)] -= bottleneck
                cap[(v, u)] += bottleneck
            flow += bottleneck
        return None if flow >= total_addons else msg

    @staticmethod
    def preview_stock_summary(items: Iterable[dict[str, Any]]) -> dict[str, int]:
        summary = {"low_stock": 0, "out_of_stock": 0, "healthy": 0}
        for item in items:
            stock = MenuDomainService.validate_stock(item.get("stock", 0), "庫存")
            if stock <= 0:
                summary["out_of_stock"] += 1
            elif stock <= LOW_STOCK:
                summary["low_stock"] += 1
            else:
                summary["healthy"] += 1
        return summary


__all__ = ["MenuDomainService", "MAX_AMOUNT", "MAX_STOCK", "LOW_STOCK"]
