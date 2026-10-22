"""資料庫中以字串保存的列舉值。集中定義，避免各處各自寫死字串。"""
from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING = 'Pending'        # 製作中
    COMPLETED = 'Completed'    # 已出餐 (待取餐)
    PICKED_UP = 'PickedUp'     # 已取餐
    CANCELLED = 'Cancelled'    # 已取消


ORDER_STATUS_LABELS = {
    OrderStatus.PENDING: '製作中',
    OrderStatus.COMPLETED: '已出餐',
    OrderStatus.PICKED_UP: '已取餐',
    OrderStatus.CANCELLED: '已取消',
}


class OrderType(StrEnum):
    DINE_IN = '內用'
    TAKE_OUT = '外帶'


class OrderSource(StrEnum):
    KIOSK = 'Kiosk'
    POS = 'POS'
    ONLINE = 'Online'   # 線上點餐 (線上端建立，同步到取餐分店出餐)
    TABLE = 'Table'     # 門市桌邊掃碼點餐 (顧客手機)


class PaymentMethod(StrEnum):
    CASH = 'Cash'
    CREDIT_CARD = 'Credit Card'
    LINE_PAY = 'Line Pay'
    JKO_PAY = '街口支付'
    APPLE_PAY = 'Apple Pay'


# 舊版前端/匯入資料曾使用的別名，統一轉成標準值後再存檔
_PAYMENT_ALIASES = {
    'CreditCard': PaymentMethod.CREDIT_CARD,
    'LinePay': PaymentMethod.LINE_PAY,
    'JKOPay': PaymentMethod.JKO_PAY,
}


def normalize_payment_method(value):
    """回傳標準付款方式；不支援的值回傳 None"""
    raw = str(value or '').strip()
    if raw in _PAYMENT_ALIASES:
        return _PAYMENT_ALIASES[raw]
    try:
        return PaymentMethod(raw)
    except ValueError:
        return None


class VoucherStatus(StrEnum):
    OPEN = 'open'          # 已兌換、尚未使用
    USED = 'used'          # 已於結帳核銷
    REFUNDED = 'refunded'  # 已退回點數


class DiscountType(StrEnum):
    FIXED = 'fixed'        # 現金折抵 (discount_value = 折抵元數)
    PERCENT = 'percent'    # 折扣 (discount_value = 應付百分比，85 代表 85 折)


class StaffRole(StrEnum):
    OWNER = 'owner'
    MANAGER = 'manager'
    STAFF = 'staff'
    KITCHEN = 'kitchen'
