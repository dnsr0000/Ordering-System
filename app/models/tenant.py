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
    # 角色預設權限以外另外授權的項目 (enums.PERMISSIONS 代碼，逗號分隔)
    permissions = db.Column(db.String(300), nullable=False, default='', server_default='')
    # 兩步驟驗證 (TOTP)：金鑰 (base32)、是否啟用、最後使用的時間窗 (防重放)、一次性備用碼 (雜湊 JSON)
    totp_secret = db.Column(db.String(64), nullable=False, default='', server_default='')
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    totp_last_counter = db.Column(db.BigInteger, nullable=False, default=0, server_default='0')
    totp_backup_codes = db.Column(db.Text, nullable=False, default='[]', server_default='[]')
    # 「登出所有裝置」時遞增：舊的登入工作階段全部失效 (同步到各分店)
    hourly_wage = db.Column(db.Integer, nullable=False, default=0, server_default='0')   # 時薪 (本機，人事成本率)
    session_epoch = db.Column(db.Integer, nullable=False, default=0, server_default='0')

    tenant = db.relationship('Tenant', backref=db.backref('staff_accounts', lazy=True))

    def has_permission(self, perm):
        from app.models.enums import ROLE_DEFAULT_PERMISSIONS
        extra = {p for p in (self.permissions or '').split(',') if p}
        return perm in ROLE_DEFAULT_PERMISSIONS.get(self.role, set()) or perm in extra

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password or '')

    @property
    def session_version(self):
        """密碼或角色變更後，舊的登入工作階段一律失效"""
        epoch = f'|{self.session_epoch}' if self.session_epoch else ''
        return hashlib.sha256(f'{self.password_hash}|{self.role}{epoch}'.encode()).hexdigest()[:16]

    def get_id(self):
        return f'{self.id}:{self.session_version}'

    @property
    def can_login(self):
        return bool(self.is_active and not self.deleted_at and self.tenant and self.tenant.is_active)


class StaffSession(db.Model):
    """員工登入工作階段 (本機資料，不同步)：裝置、IP、最後活動時間；revoked_at 非空代表已登出 / 被強制登出"""
    __tablename__ = 'staff_session'

    id = db.Column(db.Integer, primary_key=True)
    staff_account_id = db.Column(db.Integer, db.ForeignKey('staff_account.id', ondelete='CASCADE'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    last_seen = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(50), nullable=False, default='')
    user_agent = db.Column(db.String(300), nullable=False, default='')
    device = db.Column(db.String(60), nullable=False, default='')
    revoked_at = db.Column(db.DateTime, nullable=True)
    revoked_reason = db.Column(db.String(100), nullable=False, default='')

    staff = db.relationship('StaffAccount')
