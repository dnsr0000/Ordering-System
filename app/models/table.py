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

    tenant = db.relationship('Tenant')

    __table_args__ = (db.UniqueConstraint('tenant_id', 'name', name='uq_dining_table_tenant_name'),)

    @property
    def label(self):
        return f'{self.name} 桌'
