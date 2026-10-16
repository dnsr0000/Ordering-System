import io
import pickle
from datetime import datetime
import numpy as np
from sqlalchemy.types import TypeDecorator, LargeBinary
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db
from app.models.tenant import TenantScoped

FEATURE_MAGIC_PREFIX = b"NPF32v1:"

_SAFE_NUMPY_MODULES = {
    "numpy",
    "numpy.core.multiarray",
    "numpy.core.numeric",
    "numpy._core.multiarray",
    "numpy._core.numeric",
}
_SAFE_NUMPY_NAMES = {"_reconstruct", "ndarray", "dtype", "scalar", "frombuffer"}


class _RestrictedFeatureUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module in _SAFE_NUMPY_MODULES and name in _SAFE_NUMPY_NAMES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked unsafe pickle class: {module}.{name}")


def encode_feature_array(arr):
    if arr is None:
        return None
    np_arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    return FEATURE_MAGIC_PREFIX + np_arr.tobytes()


def decode_feature_bytes_safely(raw_bytes):
    if not raw_bytes:
        return None
    if isinstance(raw_bytes, np.ndarray):
        return np.asarray(raw_bytes, dtype=np.float32).reshape(-1)
    if isinstance(raw_bytes, memoryview):
        raw_bytes = raw_bytes.tobytes()
    if not isinstance(raw_bytes, (bytes, bytearray)):
        return None
    raw_bytes = bytes(raw_bytes)
    if raw_bytes.startswith(FEATURE_MAGIC_PREFIX):
        payload = raw_bytes[len(FEATURE_MAGIC_PREFIX):]
        if len(payload) == 0 or len(payload) % 4 != 0:
            return None
        return np.frombuffer(payload, dtype=np.float32).copy()
    try:
        arr = _RestrictedFeatureUnpickler(io.BytesIO(raw_bytes)).load()
        if isinstance(arr, np.ndarray):
            return np.asarray(arr, dtype=np.float32).reshape(-1)
    except Exception:
        return None
    return None


class SafeFeatureType(TypeDecorator):
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)) and bytes(value).startswith(FEATURE_MAGIC_PREFIX):
            return bytes(value)
        return encode_feature_array(value)

    def process_result_value(self, value, dialect):
        return decode_feature_bytes_safely(value)


class User(TenantScoped, db.Model):
    __tablename__ = 'user'
    __table_args__ = (
        db.Index('ux_user_tenant_phone', 'tenant_id', 'phone', unique=True),
    )
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    pin_hash = db.Column(db.String(255), nullable=True)
    photo_path = db.Column(db.String(200), nullable=False)
    feature = db.Column(SafeFeatureType, nullable=True)
    points = db.Column(db.Integer, default=0)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_logout_at = db.Column(db.DateTime, nullable=True)

    def set_pin(self, pin_str):
        if pin_str:
            self.pin_hash = generate_password_hash(str(pin_str).strip())

    def check_pin(self, pin_str):
        if not self.pin_hash or not pin_str:
            return False
        return check_password_hash(self.pin_hash, str(pin_str).strip())


class AdminLog(TenantScoped, db.Model):
    __tablename__ = 'admin_log'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(50), default='')


class StaffAuditLog(TenantScoped, db.Model):
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
    __tablename__ = 'customer_log'
    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(20), nullable=False)
    identifier = db.Column(db.String(100), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ordered_items = db.Column(db.Text, default='')
    ip_address = db.Column(db.String(50), default='')


class RewardSetting(TenantScoped, db.Model):
    __tablename__ = 'reward_setting'
    id = db.Column(db.Integer, primary_key=True)
    is_enabled = db.Column(db.Boolean, default=True)
    points_per_dollar = db.Column(db.Integer, default=1)
    max_discount_per_order = db.Column(db.Integer, default=0)
    spend_per_point = db.Column(db.Integer, default=100)


class RewardRedemption(TenantScoped, db.Model):
    __tablename__ = 'reward_redemption'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)
    points_cost = db.Column(db.Integer, nullable=False, default=0)
    is_used = db.Column(db.Boolean, default=False, nullable=False)
    is_refunded = db.Column(db.Boolean, default=False, nullable=False)
    order_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
