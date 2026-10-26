"""
哪些資料表要同步、往哪個方向、哪些欄位只屬於本機，以及重複資料的判斷依據 (自然鍵)。

方向：
- both：分店與線上雙向 (菜單、會員、優惠券、訂單…)
- up：只由分店上傳到線上 (日誌類，線上用來彙整報表)
- down：只由線上下發到分店 (總部設定；分店上傳的一律忽略)
訂單另有範圍限制：線上只把「該分店負責出餐」的訂單下發給該分店 (見 app/sync/hub.py)。
"""
from dataclasses import dataclass, field

from app.models.branch import BranchMenuOverride, StockTransfer
from app.models.coupon import Coupon, UserCoupon
from app.models.expense import Expense
from app.models.member import (GiftCard, GiftCardLedger, MemberPass, MemberTier, PassLedger, PassProduct, StampLedger,
                               WalletLedger)
from app.models.menu import CategoryTranslation, ComboOption, ComboOptionItem, MenuItem, ModifierOption
from app.models.order import Order, OrderFeedback, OrderItem
from app.models.payment import Invoice, PaymentRecord, Refund
from app.models.promotion import Promotion
from app.models.settings import HqSetting
from app.models.sync import PointLedger, SyncTombstone
from app.models.tenant import StaffAccount
from app.models.user import (AdminLog, CustomerLog, PrivacySetting, RewardRedemption, RewardSetting, StaffAuditLog,
                             User)

ALWAYS_EXCLUDED = {'id', 'tenant_id', 'sync_rev'}


@dataclass(frozen=True)
class TableSpec:
    model: type
    direction: str = 'both'
    # 只屬於本資料庫、不同步的欄位 (例如各分店自己的庫存)
    local_only: frozenset = field(default_factory=frozenset)
    # 自然鍵：新資料與既有資料的自然鍵相同時視為同一筆 (合併而非新增)
    natural_key: tuple = ()
    # 合併方式：lww (較新的 updated_at 覆蓋) / insert_only (建立後不再變更) / order / order_item / voucher / transfer
    merge: str = 'lww'
    # 每店只有一筆 (例如紅利設定)：兩邊各自建立時合併成同一筆
    singleton: bool = False
    # 沒有宣告外鍵、但存放其他資料表 id 的欄位 {欄位: 資料表}，傳送時同樣換成 uuid
    soft_refs: tuple = ()

    @property
    def name(self):
        return self.model.__tablename__


# 依外鍵相依順序排列：被參照的資料表在前
SPECS = (
    TableSpec(StaffAccount, natural_key=('username',), local_only=frozenset({'hourly_wage'})),
    TableSpec(MenuItem, local_only=frozenset({'stock', 'total_stock', 'is_sold_out', 'station_id'})),
    # 分店價格 / 分店上下架：總部設定，只下發給該分店 (見 app/sync/hub.py 的下載範圍)
    TableSpec(BranchMenuOverride, direction='down', natural_key=('node_uuid', 'menu_item_id')),
    TableSpec(CategoryTranslation, natural_key=('category',)),
    TableSpec(ModifierOption, natural_key=('category', 'name')),
    TableSpec(ComboOption),
    TableSpec(ComboOptionItem),
    TableSpec(Coupon, natural_key=('code',)),
    TableSpec(RewardSetting, singleton=True),
    TableSpec(PrivacySetting, singleton=True),
    TableSpec(Promotion),
    TableSpec(HqSetting, direction='down', natural_key=('key',)),
    TableSpec(MemberTier, natural_key=('name',)),
    TableSpec(User, local_only=frozenset({'points', 'last_login_at', 'last_logout_at', 'wallet_balance', 'total_spent',
                                          'points_notice_on', 'archived_spent'}),
              natural_key=('phone',)),
    TableSpec(UserCoupon),
    TableSpec(CustomerLog, direction='up'),
    TableSpec(Order, merge='order', local_only=frozenset({'dining_table_id', 'shift_id', 'settlement_id'})),
    # 訂單明細：新增後只有「單品作廢」會改 (作廢份數只增不減，見 engine._merge)
    TableSpec(OrderItem, merge='order_item', local_only=frozenset({'prepared_at'})),
    TableSpec(OrderFeedback, direction='up', merge='insert_only'),
    TableSpec(RewardRedemption, merge='voucher'),
    TableSpec(PointLedger, merge='insert_only'),
    TableSpec(WalletLedger, merge='insert_only'),
    TableSpec(StampLedger, merge='insert_only'),
    TableSpec(GiftCard, natural_key=('code',)),
    TableSpec(GiftCardLedger, merge='insert_only'),
    # 寄杯：商品與會員持有的寄杯以較新者為準；剩餘次數為本機欄位 (異動帳合併後重算)
    TableSpec(PassProduct),
    TableSpec(MemberPass, local_only=frozenset({'remaining'})),
    TableSpec(PassLedger, merge='insert_only'),
    # 叫貨 / 調撥單：只送給出貨方與收貨方；狀態只往前走 (見 engine._merge_transfer)
    TableSpec(StockTransfer, merge='transfer'),
    # 收款 / 退款 / 發票：分店上傳給線上端彙整報表 (各台主機自己開立、自己退款)
    TableSpec(PaymentRecord, direction='up', merge='insert_only', local_only=frozenset({'shift_id', 'settlement_id'})),
    TableSpec(Refund, direction='up'),
    TableSpec(Invoice, direction='up', local_only=frozenset({'settlement_id'})),
    # 營業費用：分店上傳給線上端 (損益表彙整 / 依分店比較)；刪除以 is_deleted 標記同步
    TableSpec(Expense, direction='up'),
    TableSpec(AdminLog, direction='up'),
    TableSpec(StaffAuditLog, direction='up', soft_refs=(
        ('staff_account_id', 'staff_account'), ('target_order_id', 'order'), ('target_user_id', 'user'))),
    TableSpec(SyncTombstone, merge='insert_only'),
)

BY_NAME = {s.name: s for s in SPECS}
BY_MODEL = {s.model: s for s in SPECS}
SYNCED_TABLE_NAMES = frozenset(BY_NAME)


def spec_for(obj_or_model):
    model = obj_or_model if isinstance(obj_or_model, type) else type(obj_or_model)
    return BY_MODEL.get(model)


def references(spec):
    """{欄位: 被參照的同步資料表}，包含外鍵與 soft_refs"""
    refs = dict(spec.soft_refs)
    for col in spec.model.__table__.columns:
        for fk in col.foreign_keys:
            if fk.column.table.name in BY_NAME:
                refs[col.name] = fk.column.table.name
    return refs


def synced_columns(spec):
    """會在網路上傳送的欄位名稱"""
    return [c.name for c in spec.model.__table__.columns if c.name not in ALWAYS_EXCLUDED | spec.local_only]
