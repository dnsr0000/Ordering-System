import secrets
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


def new_table_token():
    """QR code 網址中的桌位代碼：隨機產生、無法猜測；重新產生後舊的 QR code 立即失效"""
    return secrets.token_urlsafe(12)


class DiningTable(TenantScoped, db.Model):
    """
    門市桌位 (手機掃碼點餐)。每桌一個 QR code：/t/<token>，掃描後以該桌身分點餐，餐後到櫃檯結帳。
    桌位屬於各門市自己 (不同步到線上端或其他分店)。
    """
    __tablename__ = 'dining_table'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), nullable=False)
    token = db.Column(db.String(32), nullable=False, unique=True, index=True, default=new_table_token)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    # ---- 桌位平面圖：座位數、區域 (例：室內 / 戶外 / 二樓)、形狀、位置 (平面圖寬高的千分比，未擺放時為空) ----
    seats = db.Column(db.Integer, nullable=False, default=4, server_default='4')
    area = db.Column(db.String(20), nullable=False, default='', server_default='')
    shape = db.Column(db.String(10), nullable=False, default='square', server_default='square')   # square / round / long
    pos_x = db.Column(db.Integer, nullable=True)
    pos_y = db.Column(db.Integer, nullable=True)

    tenant = db.relationship('Tenant')

    __table_args__ = (db.UniqueConstraint('tenant_id', 'name', name='uq_dining_table_tenant_name'),)

    @property
    def label(self):
        return f'{self.name} 桌'


class TablePayment(TenantScoped, db.Model):
    """
    桌邊掃碼點餐的「線上結帳」(顧客用手機付清這一桌未結帳的訂單)。門市本機資料 (不同步)。
    pending → paid (綠界通知付款成功後完成桌位結帳) / conflict (付款時訂單已在櫃檯結帳或有異動，需退款) / failed。
    """
    __tablename__ = 'table_payment'

    id = db.Column(db.Integer, primary_key=True)
    dining_table_id = db.Column(db.Integer, db.ForeignKey('dining_table.id', ondelete='CASCADE'), nullable=False, index=True)
    order_ids = db.Column(db.String(300), nullable=False, default='')   # 逗號分隔
    amount = db.Column(db.Integer, nullable=False, default=0)
    trade_no = db.Column(db.String(20), nullable=False, unique=True)     # 送綠界的特店訂單編號
    status = db.Column(db.String(12), nullable=False, default='pending', index=True)
    provider_trade_no = db.Column(db.String(30), nullable=False, default='')
    note = db.Column(db.String(200), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    paid_at = db.Column(db.DateTime, nullable=True)

    dining_table = db.relationship('DiningTable')

    @property
    def order_id_list(self):
        return [int(x) for x in self.order_ids.split(',') if x.strip().isdigit()]
