"""
結帳與訂單狀態機 (Kiosk、POS、桌邊、線上、外送、團購共用) 的對外入口。

實作依職責拆成五個模組，這裡統一匯出 (既有的 from app.services.order_service import ... 不需修改)：
- order_pricing    優惠碼、加購配額、購物車明細解析與計價、通路價 / 服務費 / 整單折扣
- order_stock      每日取餐號、條件式扣 / 回補庫存、分店庫存快照、線上訂單的取餐分店、訂單佔用的庫存
- order_validate   結帳前的檢查 (付款方式與通路、會員、品項、促銷、付款狀態…；不寫資料庫)
- order_checkout   原子結帳 process_checkout
- order_lifecycle  狀態機 (合法流轉、角色權限)、取消回補、單品作廢、一鍵出餐、換日自動結案、今天的訂單條件

併發控制完全交給資料庫，SQLite (本機) 與 PostgreSQL (線上) 行為一致：
- 取餐號：每店每日一列計數，條件式 UPDATE 取號 (同時結帳自然排隊)。
- 庫存 / 點數 / 憑證 / 優惠券：皆為「WHERE 剩餘量足夠」的條件式 UPDATE，rowcount 為 0 即失敗。
- 訂單狀態：WHERE status = 目前狀態 的 CAS 更新，同一張單同時操作只有一方成功。
- 重送：前端每次結帳產生一個 client_order_id (存為 Order.client_order_id，同店唯一索引)，重送時直接回傳同一張訂單；
  Order.uuid 由伺服器產生 (追蹤頁憑證)。
"""
from app.domain.order.service import OrderDomainService
from app.services.order_pricing import (  # noqa: F401
    NON_MAIN_ADDON_CATEGORIES, _PRICE_TAG, _COMBO_TAG, PromoResult, normalize_code, find_promo, _addon_qualifies,
    _validate_addon_quota, CheckoutLine, _clean_customization, _combo_choice_sides, _check_combo_sides,
    _combo_sides_snapshot, _parse_lines, _lock_member, _consumed_promo_error, _apply_promo, _service_charge_applies,
    _price_channel, _manual_discount,
)
from app.services.order_stock import (  # noqa: F401
    PICKUP_COUNTER, MAX_PICKUP_NUMBER, next_pickup_number, _change_stock, _deduct_stock, consume_stock_unchecked,
    _change_node_stock, online_pickup_node, order_stock_demands, restock_demands,
    _day_start_hour, business_day,
)
from app.services.order_validate import _validate  # noqa: F401
from app.services.order_checkout import (  # noqa: F401
    Checkout, _after_commit, _persist, _prepare, _price, _replay, _reserve, find_by_client_order_id,
    process_checkout, _DiscountApprovalNeeded, _release_waiting_invoices,
)
from app.services.order_lifecycle import (  # noqa: F401
    LEGAL_TRANSITIONS, _ALL_TRANSITIONS, ROLE_TRANSITIONS, change_order_status, system_cancel, void_item,
    flag_refund_if_paid, _rollback_cancelled_order, complete_all_pending_orders, auto_close_stale_orders,
    _auto_close_done_for, ensure_daily_auto_close, todays_range, todays_orders_condition,
    _is_pre_ledger_order,
)

order_domain_service = OrderDomainService()


def create_order(payload: dict, tenant_id: int) -> dict:
    """Create a draft order via the domain layer before persistence."""
    return order_domain_service.create_order(payload, tenant_id)


def transition_order_status(order: dict, next_status: str) -> dict:
    """Validate and apply status transitions using the business rules in the domain layer."""
    return order_domain_service.transition_status(order, next_status)


def validate_cancel_allowed(order: dict) -> None:
    """Refuse cancellation when the order is already completed or cancelled."""
    order_domain_service.validate_cancel_allowed(order)


def validate_payment_status(status: str) -> str:
    """Normalize and validate payment status using the domain contract."""
    return order_domain_service.validate_payment_status(status)


def validate_expected_total(subtotal: float, discount: float, *, service_charge: float = 0, delivery_fee: float = 0,
                           expected_total: str | int | float | None = None) -> float:
    """Validate that the server-calculated total matches the client expectation."""
    return order_domain_service.validate_expected_total(
        subtotal,
        discount,
        service_charge=service_charge,
        delivery_fee=delivery_fee,
        expected_total=expected_total,
    )


def calculate_total(items: list[dict]) -> float:
    """Compute order total using the domain rules for quantity and pricing."""
    return order_domain_service.calculate_total(items)


__all__ = [
    'NON_MAIN_ADDON_CATEGORIES', 'PromoResult', 'normalize_code', 'find_promo', 'CheckoutLine', 'PICKUP_COUNTER',
    'MAX_PICKUP_NUMBER', 'next_pickup_number', 'consume_stock_unchecked', 'online_pickup_node', 'order_stock_demands',
    'restock_demands', 'business_day',
    'process_checkout', 'find_by_client_order_id', 'LEGAL_TRANSITIONS', 'ROLE_TRANSITIONS', 'change_order_status', 'system_cancel', 'void_item',
    'flag_refund_if_paid', 'complete_all_pending_orders', 'auto_close_stale_orders', 'ensure_daily_auto_close',
    'todays_range', 'todays_orders_condition', 'create_order', 'transition_order_status', 'validate_cancel_allowed',
    'validate_payment_status', 'validate_expected_total', 'calculate_total',
]
