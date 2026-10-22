from datetime import datetime

import numpy as np
from sqlalchemy.types import LargeBinary, TypeDecorator
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.enums import VoucherStatus
from app.models.mixins import TenantScoped

# 人臉特徵儲存格式：NPF32v1: + 128 個 float32 (SFace 輸出固定 128 維)
FEATURE_MAGIC_PREFIX = b"NPF32v1:"
FEATURE_DIM = 128


def as_valid_feature(arr):
    """轉為一維 float32；維度不是 FEATURE_DIM 或含 NaN/Inf 時回傳 None"""
    try:
        vec = np.asarray(arr, dtype=np.float32).reshape(-1)
    except Exception:
        return None
    if vec.shape[0] != FEATURE_DIM or not np.all(np.isfinite(vec)):
        return None
    return vec


def encode_feature_array(arr):
    if arr is None:
        return None
    vec = as_valid_feature(arr)
    if vec is None:
        raise ValueError(f"人臉特徵向量必須為 {FEATURE_DIM} 維且數值皆為有限值")
    return FEATURE_MAGIC_PREFIX + vec.tobytes()


def decode_feature_bytes(raw):
    """解碼 NPF32v1 特徵；格式、長度或數值不合法時回傳 None (舊版 Pickle 由 legacy 升級程序一次轉換)"""
    if raw is None:
        return None
    raw = bytes(raw)
    if not raw.startswith(FEATURE_MAGIC_PREFIX):
        return None
    payload = raw[len(FEATURE_MAGIC_PREFIX):]
    if len(payload) != FEATURE_DIM * 4:
        return None
    return as_valid_feature(np.frombuffer(payload, dtype=np.float32).copy())


class FeatureVector(TypeDecorator):
    """人臉特徵欄位：ORM 層存取 numpy 陣列，資料庫中為 NPF32v1 二進位"""
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        return encode_feature_array(value)

    def process_result_value(self, value, dialect):
        return decode_feature_bytes(value)


class User(TenantScoped, db.Model):
    """會員：手機 + PIN 或人臉登入，累積紅利點數。同一店家內手機唯一。"""
    __tablename__ = 'user'
    __table_args__ = (db.Index('ux_user_tenant_phone', 'tenant_id', 'phone', unique=True),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    pin_hash = db.Column(db.String(255), nullable=True)
    photo_path = db.Column(db.String(200), nullable=False, default='')
    feature = db.Column(FeatureVector, nullable=True)
    points = db.Column(db.Integer, nullable=False, default=0)
    # 個資告知與同意 (個資法第 8 條告知、第 7 條同意的舉證)：同意的告知版本與時間；人臉辨識另行同意，可隨時撤回
    privacy_consent_version = db.Column(db.String(20), nullable=False, default='', server_default='')
    privacy_consented_at = db.Column(db.DateTime, nullable=True)
    face_consented_at = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_logout_at = db.Column(db.DateTime, nullable=True)

    def set_pin(self, pin):
        self.pin_hash = generate_password_hash(str(pin).strip())

    def check_pin(self, pin):
        return bool(self.pin_hash) and check_password_hash(self.pin_hash, str(pin or '').strip())


class AdminLog(TenantScoped, db.Model):
    """後台登入/登出紀錄"""
    __tablename__ = 'admin_log'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(50), default='')


class StaffAuditLog(TenantScoped, db.Model):
    """員工操作稽核 (取消訂單、改點數、上下架…)"""
    __tablename__ = 'staff_audit_log'

    id = db.Column(db.Integer, primary_key=True)
    staff_account_id = db.Column(db.Integer, nullable=True)
    staff_username = db.Column(db.String(100), nullable=False, default='')
    staff_role = db.Column(db.String(30), nullable=False, default='staff')
    action_type = db.Column(db.String(60), nullable=False)
    target_order_id = db.Column(db.Integer, nullable=True)
    target_user_id = db.Column(db.Integer, nullable=True)
    points_delta = db.Column(db.Integer, default=0)
    details = db.Column(db.Text, default='')
    ip_address = db.Column(db.String(50), default='')
    created_at = db.Column(db.DateTime, default=datetime.now)


class CustomerLog(TenantScoped, db.Model):
    """顧客進場紀錄：一次 kiosk 使用 (會員或訪客) 一筆，訪客的「我的訂單」以此歸戶"""
    __tablename__ = 'customer_log'

    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(20), nullable=False)
    identifier = db.Column(db.String(100), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ordered_items = db.Column(db.Text, default='')
    ip_address = db.Column(db.String(50), default='')


class RewardSetting(TenantScoped, db.Model):
    """紅利規則 (每店一筆)：幾點折 $1、單筆折抵上限、每消費幾元送 1 點"""
    __tablename__ = 'reward_setting'
    __table_args__ = (db.Index('ux_reward_setting_tenant', 'tenant_id', unique=True),)

    id = db.Column(db.Integer, primary_key=True)
    is_enabled = db.Column(db.Boolean, default=True)
    points_per_dollar = db.Column(db.Integer, default=1)
    max_discount_per_order = db.Column(db.Integer, default=0)
    spend_per_point = db.Column(db.Integer, default=100)


class RewardRedemption(TenantScoped, db.Model):
    """
    紅利商品兌換憑證。status：open (已扣點、尚未使用) → used (結帳核銷) 或 refunded (退回點數)。
    狀態只能經由 voucher_service 的條件式 UPDATE 轉換，確保核銷與退點只有一方成功。
    """
    __tablename__ = 'reward_redemption'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='SET NULL'), nullable=True)
    points_cost = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default=VoucherStatus.OPEN)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class PrivacySetting(TenantScoped, db.Model):
    """隱私權告知頁上的營運者資訊 (每店一筆，後台設定；未填時以店家名稱代入)"""
    __tablename__ = 'privacy_setting'
    __table_args__ = (db.Index('ux_privacy_setting_tenant', 'tenant_id', unique=True),)

    id = db.Column(db.Integer, primary_key=True)
    operator_name = db.Column(db.String(120), nullable=False, default='')
    contact_email = db.Column(db.String(120), nullable=False, default='')
    contact_phone = db.Column(db.String(50), nullable=False, default='')
    contact_address = db.Column(db.String(200), nullable=False, default='')
    # 會員停止使用多久後刪除個人資料 (月)
    retention_months = db.Column(db.Integer, nullable=False, default=24)
