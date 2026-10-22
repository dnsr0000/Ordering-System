import hashlib
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.mixins import SyncMixin


class Tenant(SyncMixin, db.Model):
    """店家 (租戶)。slug 用於 /shop/<slug> 店家入口。"""
    __tablename__ = 'tenant'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    # 桌位 QR code 使用的門市網址 (顧客手機連店內 Wi-Fi 時能開啟的網址，例如 http://192.168.1.10:5000)
    table_qr_base_url = db.Column(db.String(200), nullable=False, default='', server_default='')


class StaffAccount(UserMixin, SyncMixin, db.Model):
    """
    員工 / 後台帳號 (Flask-Login 的登入主體)。
    role：owner / manager / staff / kitchen。
    deleted_at 非空代表已軟刪除：保留列讓稽核紀錄永遠對得到帳號，id 也不會被重用。
    """
    __tablename__ = 'staff_account'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default='staff')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    deleted_at = db.Column(db.DateTime, nullable=True)

    tenant = db.relationship('Tenant', backref=db.backref('staff_accounts', lazy=True))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password or '')

    @property
    def session_version(self):
        """密碼或角色變更後，舊的登入工作階段一律失效"""
        return hashlib.sha256(f'{self.password_hash}|{self.role}'.encode()).hexdigest()[:16]

    def get_id(self):
        return f'{self.id}:{self.session_version}'

    @property
    def can_login(self):
        return bool(self.is_active and not self.deleted_at and self.tenant and self.tenant.is_active)
