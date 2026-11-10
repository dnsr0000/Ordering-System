"""
結帳與訂單狀態機 (Kiosk、POS、桌邊、線上、外送、團購共用) 的對外入口。

純 Facade 模組：只負責匯出五個子模組的物件，保持 100% 向後相容，內部不包含任何函式實作 (無 def)。
"""
from app.services.order_pricing import (  # noqa: F401
    NON_MAIN_ADDON_CATEGORIES, _PRICE_TAG, _COMBO_TAG, PromoResult, normalize_code, find_promo, _addon_qualifies,
    _validate_addon_quota, CheckoutLine, _clean_customization, _combo_choice_sides, _check_combo_sides,
    _combo_sides_snapshot, _parse_lines, _lock_member, _consumed_promo_error, _apply_promo, _service_charge_applies,
    _price_channel, _manual_discount, _norm_category, _is_main_category,
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

__all__ = [
    'NON_MAIN_ADDON_CATEGORIES', '_PRICE_TAG', '_COMBO_TAG', 'PromoResult', 'normalize_code', 'find_promo',
    '_addon_qualifies', '_validate_addon_quota', 'CheckoutLine', '_clean_customization', '_combo_choice_sides',
    '_check_combo_sides', '_combo_sides_snapshot', '_parse_lines', '_lock_member', '_consumed_promo_error',
    '_apply_promo', '_service_charge_applies', '_price_channel', '_manual_discount', '_norm_category',
    '_is_main_category', 'PICKUP_COUNTER', 'MAX_PICKUP_NUMBER', 'next_pickup_number', '_change_stock',
    '_deduct_stock', 'consume_stock_unchecked', '_change_node_stock', 'online_pickup_node', 'order_stock_demands',
    'restock_demands', '_day_start_hour', 'business_day', '_validate', 'Checkout', '_after_commit', '_persist',
    '_prepare', '_price', '_replay', '_reserve', 'find_by_client_order_id', 'process_checkout',
    '_DiscountApprovalNeeded', '_release_waiting_invoices', 'LEGAL_TRANSITIONS', '_ALL_TRANSITIONS',
    'ROLE_TRANSITIONS', 'change_order_status', 'system_cancel', 'void_item', 'flag_refund_if_paid',
    '_rollback_cancelled_order', 'complete_all_pending_orders', 'auto_close_stale_orders', '_auto_close_done_for',
    'ensure_daily_auto_close', 'todays_range', 'todays_orders_condition', '_is_pre_ledger_order',
]