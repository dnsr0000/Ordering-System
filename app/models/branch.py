"""
多分店營運：分店價格 / 分店上下架 (總部設定、只下發給該分店)、分店叫貨與調撥單 (分店 / 總部中央廚房之間)。
"""
import json
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped

HQ_SITE = 'HQ'   # 總部 / 中央廚房 (線上端主機自己的原料庫存)


def active_overrides():
    """
    目前請求套用的分店價格 / 上下架 {menu_item_id: (價差, 是否下架)}：點餐頁、POS、結帳時由
    app.services.branch_service.activate() 依分店設定；其他頁面 (後台菜單編輯) 為空，顯示總部價格。
    """
    from flask import g, has_app_context
    if not has_app_context():
        return {}
    return g.get('branch_menu') or {}


class BranchMenuOverride(TenantScoped, db.Model):
    """
    分店價格 / 分店上下架：總部 (線上端) 為某家分店設定某個餐點的售價或下架。
    price 為該分店的內用價 (空白 = 與總部同價)；通路價、特價依「分店價 - 總部價」的差額同步調整。
    同步方向只有線上端 → 該分店 (其他分店收不到；分店上傳的會被拒絕)。
    清除設定時把 price 設為空白、is_hidden 設為 False (不刪除，避免同一組分店 / 餐點的新舊資料互相衝突)。
    """
    __tablename__ = 'branch_menu_override'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'node_uuid', 'menu_item_id', name='uq_branch_menu_override'),)

    id = db.Column(db.Integer, primary_key=True)
    node_uuid = db.Column(db.String(36), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False, index=True)
    price = db.Column(db.Integer, nullable=True)
    is_hidden = db.Column(db.Boolean, nullable=False, default=False)
    changed_by = db.Column(db.String(120), nullable=False, default='')

    menu_item = db.relationship('MenuItem')


TRANSFER_STATUS_LABELS = {'requested': '待出貨', 'rejected': '已拒絕', 'cancelled': '已取消', 'shipped': '已出貨',
                          'received': '已收貨'}
# 同步合併時狀態只往前走：數字大的覆蓋小的；同一級 (取消 / 拒絕) 以較新的為準
TRANSFER_RANK = {'requested': 0, 'cancelled': 1, 'rejected': 1, 'shipped': 2, 'received': 3}


class StockTransfer(TenantScoped, db.Model):
    """
    分店叫貨 / 調撥單 (原料)：
    - request 叫貨：收貨方 (分店) 建立 → 出貨方 (總部中央廚房或另一家分店) 出貨或拒絕 → 收貨方收貨入庫
    - transfer 調撥：出貨方直接建立並出貨 (當下扣庫存) → 收貨方收貨入庫
    出貨 / 收貨在各自的主機上執行並增減自己的原料庫存 (原料依名稱對應；收貨方沒有這項原料時自動建立)，
    單據本身經線上端同步給雙方。site 為分店 uuid，總部為 'HQ'。
    """
    __tablename__ = 'stock_transfer'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False, default='request')        # request / transfer
    from_site = db.Column(db.String(36), nullable=False, index=True)
    to_site = db.Column(db.String(36), nullable=False, index=True)
    from_name = db.Column(db.String(100), nullable=False, default='')
    to_name = db.Column(db.String(100), nullable=False, default='')
    status = db.Column(db.String(12), nullable=False, default='requested', index=True)
    # [{name, unit, qty (叫貨 / 調撥數量), shipped (實出), received (實收), unit_cost (出貨方單位成本)}]
    lines_json = db.Column(db.Text, nullable=False, default='[]')
    note = db.Column(db.String(200), nullable=False, default='')
    created_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    shipped_by = db.Column(db.String(120), nullable=False, default='')
    shipped_at = db.Column(db.DateTime, nullable=True)
    received_by = db.Column(db.String(120), nullable=False, default='')
    received_at = db.Column(db.DateTime, nullable=True)
    reason = db.Column(db.String(200), nullable=False, default='')             # 拒絕 / 取消原因、收貨差異說明

    @property
    def lines(self):
        try:
            rows = json.loads(self.lines_json or '[]')
        except ValueError:
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def set_lines(self, rows):
        self.lines_json = json.dumps(rows, ensure_ascii=False)

    @property
    def number(self):
        prefix = 'RQ' if self.kind == 'request' else 'TR'
        return f'{prefix}{self.created_at:%y%m%d}-{(self.uuid or "")[:4].upper()}' if self.created_at else ''

    @property
    def status_label(self):
        return TRANSFER_STATUS_LABELS.get(self.status, self.status)

    @property
    def shipped_value(self):
        return round(sum((ln.get('shipped') or 0) * (ln.get('unit_cost') or 0) for ln in self.lines))

    def to_dict(self):
        return {'id': self.id, 'uuid': self.uuid, 'number': self.number, 'kind': self.kind, 'from_site': self.from_site,
                'to_site': self.to_site, 'from_name': self.from_name, 'to_name': self.to_name, 'status': self.status,
                'status_label': self.status_label, 'lines': self.lines, 'note': self.note, 'reason': self.reason,
                'created_by': self.created_by, 'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else ''}
