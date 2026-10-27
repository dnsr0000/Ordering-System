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
    DELIVERY = '外送'   # 外送平台訂單 (Uber Eats / foodpanda / 其他平台手動登錄)
    HOME_DELIVERY = '外送到府'   # 本店自有外送 (線上點餐 / POS 電話訂餐，本店人員配送)


class OrderSource(StrEnum):
    KIOSK = 'Kiosk'
    POS = 'POS'
    ONLINE = 'Online'   # 線上點餐 (線上端建立，同步到取餐分店出餐)
    TABLE = 'Table'     # 門市桌邊掃碼點餐 (顧客手機)
    UBER_EATS = 'UberEats'
    FOODPANDA = 'foodpanda'
    DELIVERY = 'Delivery'   # 其他外送平台 (POS 手動登錄)


# 自有外送的配送狀態 (只往前走；delivered / failed 為結束)
DELIVERY_STATUS_LABELS = {'pending': '待派車', 'assigned': '已指派外送員', 'out': '配送中', 'delivered': '已送達',
                          'failed': '配送失敗'}
DELIVERY_STATUS_RANK = {'': 0, 'pending': 1, 'assigned': 2, 'out': 3, 'delivered': 4, 'failed': 4}

DELIVERY_SOURCES = (OrderSource.UBER_EATS, OrderSource.FOODPANDA, OrderSource.DELIVERY)


class PaymentMethod(StrEnum):
    CASH = 'Cash'
    CREDIT_CARD = 'Credit Card'
    LINE_PAY = 'Line Pay'
    JKO_PAY = '街口支付'
    APPLE_PAY = 'Apple Pay'
    STORED_VALUE = '儲值金'      # 會員儲值金扣款
    ATM = 'ATM 轉帳'             # 線上點餐：綠界 ATM 虛擬帳號
    CVS = '超商代碼'             # 線上點餐：綠界超商代碼 / 條碼繳費
    GIFT_CARD = '禮物卡'         # 本店禮物卡 (商品禮券) 扣款
    PLATFORM = '平台代收'        # 外送平台代收 (平台結算後撥款)
    SPLIT = '混合付款'           # POS 一筆訂單分多種方式付款 (明細在收款紀錄 payment_record)


PAYMENT_METHOD_LABELS = {
    PaymentMethod.CASH: '現金',
    PaymentMethod.CREDIT_CARD: '信用卡',
    PaymentMethod.LINE_PAY: 'LINE Pay',
    PaymentMethod.JKO_PAY: '街口支付',
    PaymentMethod.APPLE_PAY: 'Apple Pay',
    PaymentMethod.STORED_VALUE: '儲值金',
    PaymentMethod.PLATFORM: '平台代收',
    PaymentMethod.ATM: 'ATM 轉帳',
    PaymentMethod.CVS: '超商代碼',
    PaymentMethod.GIFT_CARD: '禮物卡',
    PaymentMethod.SPLIT: '混合付款',
}


# 舊版前端/匯入資料曾使用的別名，統一轉成標準值後再存檔
_PAYMENT_ALIASES = {
    'CreditCard': PaymentMethod.CREDIT_CARD,
    'LinePay': PaymentMethod.LINE_PAY,
    'JKOPay': PaymentMethod.JKO_PAY,
    'StoredValue': PaymentMethod.STORED_VALUE,
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


# 員工權限 (角色預設之外，負責人可在員工帳號頁個別授權)
PERMISSIONS = {
    'discount': '整單折扣 / 改價 (免主管核准)',
    'refund': '作廢 / 退款 (免主管核准)',
    'cash_drawer': '開班 / 關班 / 零用金',
    'reports': '查看營運報表',
    'inventory': '原物料進貨 / 盤點',
    'reservations': '訂位 / 候位',
    'wallet': '會員儲值',
    'delivery': '外送平台訂單登錄',
}
ROLE_DEFAULT_PERMISSIONS = {
    'owner': set(PERMISSIONS),
    'manager': set(PERMISSIONS),
    'staff': {'cash_drawer', 'reservations', 'wallet', 'delivery'},
    'kitchen': {'inventory'},
}


class StaffRole(StrEnum):
    OWNER = 'owner'
    MANAGER = 'manager'
    STAFF = 'staff'
    KITCHEN = 'kitchen'
