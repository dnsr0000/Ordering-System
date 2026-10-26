"""
對外整合 (第三輪 #14)：API 金鑰、Webhook 端點與送出紀錄；裝置管理 (#15)。門市 / 線上端本機資料，不同步。
"""
import hashlib
import secrets
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped

API_SCOPES = {
    'orders:read': '讀取訂單', 'menu:read': '讀取菜單', 'menu:write': '更新餐點售完 / 庫存',
    'members:read': '查詢會員 (姓名、點數、等級)', 'reports:read': '讀取每日營業統計',
}
WEBHOOK_EVENTS = {
    'order.created': '新訂單', 'order.paid': '訂單付款完成', 'order.status_changed': '訂單狀態改變 (製作中 / 可取餐 / 已取餐)',
    'order.cancelled': '訂單取消',
}


def hash_secret(raw):
    return hashlib.sha256(str(raw or '').encode()).hexdigest()


class ApiKey(TenantScoped, db.Model):
    """API 金鑰：只在建立時顯示一次，資料庫存雜湊；scopes 以逗號分隔"""
    __tablename__ = 'api_key'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    prefix = db.Column(db.String(12), nullable=False, default='')
    key_hash = db.Column(db.String(64), nullable=False, unique=True)
    scopes = db.Column(db.String(200), nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    last_used_at = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(50), nullable=False, default='')

    @property
    def scope_list(self):
        return [s for s in (self.scopes or '').split(',') if s in API_SCOPES]

    def issue(self):
        raw = 'kk_' + secrets.token_urlsafe(32)
        self.key_hash, self.prefix = hash_secret(raw), raw[:10]
        return raw


class WebhookEndpoint(TenantScoped, db.Model):
    __tablename__ = 'webhook_endpoint'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    url = db.Column(db.String(300), nullable=False)
    secret = db.Column(db.String(64), nullable=False, default=lambda: secrets.token_hex(24))
    events = db.Column(db.String(200), nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    fail_streak = db.Column(db.Integer, nullable=False, default=0)
    last_status = db.Column(db.String(120), nullable=False, default='')
    last_sent_at = db.Column(db.DateTime, nullable=True)

    @property
    def event_list(self):
        return [e for e in (self.events or '').split(',') if e in WEBHOOK_EVENTS]


class WebhookDelivery(TenantScoped, db.Model):
    """status：pending 待送 (含重試) / sent 已送達 / failed 放棄"""
    __tablename__ = 'webhook_delivery'

    id = db.Column(db.Integer, primary_key=True)
    endpoint_id = db.Column(db.Integer, db.ForeignKey('webhook_endpoint.id', ondelete='CASCADE'), nullable=False, index=True)
    event = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.Text, nullable=False, default='{}')
    status = db.Column(db.String(10), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    response_code = db.Column(db.Integer, nullable=True)
    last_error = db.Column(db.String(300), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    sent_at = db.Column(db.DateTime, nullable=True)

    endpoint = db.relationship('WebhookEndpoint')


class WebhookOrderState(db.Model):
    """Webhook 掃描器記住的訂單狀態 (比對出新訂單 / 狀態改變 / 付款完成 / 取消)"""
    __tablename__ = 'webhook_order_state'

    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='CASCADE'), primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='')
    payment_status = db.Column(db.String(20), nullable=False, default='')
    seen_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


DEVICE_KINDS = {'kiosk': '點餐機', 'pos': 'POS 收銀', 'kitchen': '廚房看板', 'display': '叫號 / 客顯', 'other': '其他'}


class Device(TenantScoped, db.Model):
    """
    門市裝置 (點餐機 / POS / 廚房看板…)：後台建立後在裝置上輸入配對碼，瀏覽器記住裝置權杖 (Cookie)。
    裝置頁面每分鐘回報心跳 (版本、目前頁面)；後台可遠端鎖定 (畫面蓋上鎖定訊息、無法送出操作) 或要求重新整理。
    """
    __tablename__ = 'device'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    kind = db.Column(db.String(10), nullable=False, default='kiosk')
    token_hash = db.Column(db.String(64), nullable=True, unique=True)
    pair_code = db.Column(db.String(8), nullable=True, index=True)
    pair_expires_at = db.Column(db.DateTime, nullable=True)
    paired_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(50), nullable=False, default='')
    user_agent = db.Column(db.String(200), nullable=False, default='')
    app_version = db.Column(db.String(20), nullable=False, default='')
    last_path = db.Column(db.String(120), nullable=False, default='')
    is_locked = db.Column(db.Boolean, nullable=False, default=False)
    lock_message = db.Column(db.String(120), nullable=False, default='')
    reload_requested_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    @property
    def kind_label(self):
        return DEVICE_KINDS.get(self.kind, self.kind)

    def is_online(self, now=None, seconds=180):
        now = now or datetime.now()
        return bool(self.last_seen_at) and (now - self.last_seen_at).total_seconds() <= seconds
