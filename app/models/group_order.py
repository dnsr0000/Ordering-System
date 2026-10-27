"""
團體預購 (第四輪)：主揪建立團購連結 → 同事各自填寫要點的餐點 → 截止時合併成一張預約取餐訂單 (現場付款)。
門市 / 單機的本機資料 (不同步；送出後的訂單照常同步)。
"""
import hashlib
import json
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped

GROUP_STATUS_LABELS = {'open': '收單中', 'submitting': '送單中', 'submitted': '已送單', 'failed': '送單失敗',
                       'cancelled': '已取消'}


def hash_key(raw):
    return hashlib.sha256(str(raw or '').encode()).hexdigest()


class GroupOrder(TenantScoped, db.Model):
    __tablename__ = 'group_order'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(32), nullable=False, unique=True, index=True)       # 分享連結 /group/<token>
    manage_key_hash = db.Column(db.String(64), nullable=False)                     # 主揪管理連結的金鑰 (只存雜湊)
    title = db.Column(db.String(60), nullable=False)
    organizer_name = db.Column(db.String(30), nullable=False)
    organizer_phone = db.Column(db.String(20), nullable=False)
    order_type = db.Column(db.String(10), nullable=False, default='外帶')
    pickup_at = db.Column(db.DateTime, nullable=False, index=True)
    deadline = db.Column(db.DateTime, nullable=False, index=True)
    auto_submit = db.Column(db.Boolean, nullable=False, default=True)               # 截止時自動送單
    status = db.Column(db.String(12), nullable=False, default='open', index=True)
    note = db.Column(db.String(100), nullable=False, default='')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    error = db.Column(db.String(200), nullable=False, default='')
    created_by = db.Column(db.String(120), nullable=False, default='')              # 店員代建時的帳號
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    submitted_at = db.Column(db.DateTime, nullable=True)

    entries = db.relationship('GroupOrderEntry', back_populates='group', cascade='all, delete-orphan',
                              order_by='GroupOrderEntry.id')
    order = db.relationship('Order')

    @property
    def status_label(self):
        return GROUP_STATUS_LABELS.get(self.status, self.status)

    @property
    def is_open(self):
        return self.status == 'open' and datetime.now() < self.deadline


class GroupOrderEntry(TenantScoped, db.Model):
    """一位參加者的點餐：items 為 [{menu_item_id, quantity, customization}]"""
    __tablename__ = 'group_order_entry'

    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey('group_order.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(30), nullable=False)
    items = db.Column(db.Text, nullable=False, default='[]')
    note = db.Column(db.String(100), nullable=False, default='')
    entry_key_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    group = db.relationship('GroupOrder', back_populates='entries')

    @property
    def lines(self):
        try:
            data = json.loads(self.items or '[]')
        except ValueError:
            return []
        return data if isinstance(data, list) else []
