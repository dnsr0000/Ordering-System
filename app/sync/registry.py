"""
哪些資料表要同步、往哪個方向、哪些欄位只屬於本機，以及重複資料的判斷依據 (自然鍵)。

方向：
- both：分店與線上雙向 (菜單、會員、優惠券、訂單…)
- up：只由分店上傳到線上 (日誌類，線上用來彙整報表)
訂單另有範圍限制：線上只把「該分店負責出餐」的訂單下發給該分店 (見 app/sync/hub.py)。
"""
from dataclasses import dataclass, field

from app.models.coupon import Coupon, UserCoupon
from app.models.menu import ComboOption, ComboOptionItem, MenuItem, ModifierOption
from app.models.order import Order, OrderItem
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
    # 合併方式：lww (較新的 updated_at 覆蓋) / insert_only (建立後不再變更) / order / voucher
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
    TableSpec(StaffAccount, natural_key=('username',)),
    TableSpec(MenuItem, local_only=frozenset({'stock', 'total_stock', 'is_sold_out'})),
    TableSpec(ModifierOption, natural_key=('category', 'name')),
    TableSpec(ComboOption),
    TableSpec(ComboOptionItem),
    TableSpec(Coupon, natural_key=('code',)),
    TableSpec(RewardSetting, singleton=True),
    TableSpec(PrivacySetting, singleton=True),
    TableSpec(User, local_only=frozenset({'points', 'last_login_at', 'last_logout_at'}), natural_key=('phone',)),
    TableSpec(UserCoupon),
    TableSpec(CustomerLog, direction='up'),
    TableSpec(Order, merge='order', local_only=frozenset({'dining_table_id'})),
    TableSpec(OrderItem, merge='insert_only'),
    TableSpec(RewardRedemption, merge='voucher'),
    TableSpec(PointLedger, merge='insert_only'),
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
