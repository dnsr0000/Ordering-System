import io
import pickle
from datetime import datetime
import numpy as np
from sqlalchemy.types import TypeDecorator, LargeBinary
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db
from app.models.tenant import TenantScoped

# 安全特徵向量 Magic Header，格式：NPF32v1: + 128 維 float32 bytes
FEATURE_MAGIC_PREFIX = b"NPF32v1:"

# 白名單限制 Unpickler 所允許載入的模組與類別
_SAFE_NUMPY_MODULES = {
    "numpy",
    "numpy.core.multiarray",
    "numpy.core.numeric",
    "numpy._core.multiarray",
    "numpy._core.numeric",
}
_SAFE_NUMPY_NAMES = {"_reconstruct", "ndarray", "dtype", "scalar", "frombuffer"}


class _RestrictedFeatureUnpickler(pickle.Unpickler):
    """受限安全反序列化器，僅允許載入白名單內的 NumPy 資料結構"""
    def find_class(self, module, name):
        if module in _SAFE_NUMPY_MODULES and name in _SAFE_NUMPY_NAMES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked unsafe pickle class: {module}.{name}")


def encode_feature_array(arr):
    """將 NumPy 特徵向量序列化為帶有 Magic Header 的安全二進位字串"""
    if arr is None:
        return None
    np_arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    return FEATURE_MAGIC_PREFIX + np_arr.tobytes()


def decode_feature_bytes_safely(raw_bytes):
    """
    安全還原特徵向量二進位資料為 NumPy float32 一維陣列：
    1. 優先以 NPF32v1 安全位元組流直接載入
    2. 針對舊版資料庫歷史 Pickle 資料，透過受限白名單反序列化讀取
    """
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
    """SQLAlchemy 自訂資料庫欄位型別，對接 LargeBinary 並在 ORM 層透明加解密"""
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
    """
    會員資料主表:儲存會員基本資料、手機登入 PIN 碼、人臉辨識特徵向量與現有累積紅利點數
    """
    __tablename__ = 'user'

    # 約束條件：同店家下手機號碼具備唯一索引，防止多租戶環境跨店或店內重複註冊
    __table_args__ = (
        db.Index('ux_user_tenant_phone', 'tenant_id', 'phone', unique=True),
    )

    # 主鍵：會員編號 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 會員真實姓名/稱呼 (VARCHAR(100)，不可為空)
    name = db.Column(db.String(100), nullable=False)

    # 會員手機號碼 (VARCHAR(50)，不可為空，登入與比對鍵)
    phone = db.Column(db.String(50), nullable=False)

    # 會員手機登入 4~6 碼 PIN 密碼雜湊值 (VARCHAR(255)，經 Werkzeug 加密)
    pin_hash = db.Column(db.String(255), nullable=True)

    # 會員照片檔名/相對路徑 (VARCHAR(200)，實體存於 static/member/ 隨機 UUID 檔名)
    photo_path = db.Column(db.String(200), nullable=False)

    # 人臉特徵向量二進位資料 (自訂 SafeFeatureType，128 維 float32)
    feature = db.Column(SafeFeatureType, nullable=True)

    # 會員當前可用紅利點數 (INTEGER，預設 0)
    points = db.Column(db.Integer, default=0)

    # 最後登入成功時間 (DATETIME)
    last_login_at = db.Column(db.DateTime, nullable=True)

    # 最後登出時間 (DATETIME)
    last_logout_at = db.Column(db.DateTime, nullable=True)

    def set_pin(self, pin_str):
        """將純數字 PIN 進行 Hash 雜湊處理後寫入"""
        if pin_str:
            self.pin_hash = generate_password_hash(str(pin_str).strip())

    def check_pin(self, pin_str):
        """核對輸入之 PIN 碼是否相符"""
        if not self.pin_hash or not pin_str:
            return False
        return check_password_hash(self.pin_hash, str(pin_str).strip())


class AdminLog(TenantScoped, db.Model):
    """
    後台管理員登入日誌表:追蹤後台負責人與店長之登入、登出時間與來源 IP 位址
    """
    __tablename__ = 'admin_log'

    # 主鍵：日誌編號 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 登入管理員帳號 (VARCHAR(50)，不可為空)
    username = db.Column(db.String(50), nullable=False)

    # 登入時間 (DATETIME，預設當前時間)
    login_at = db.Column(db.DateTime, default=datetime.now)

    # 登出時間 (DATETIME，點擊登出後更新，未主動登出則為空)
    logout_at = db.Column(db.DateTime, nullable=True)

    # 登入來源客戶端 IP (VARCHAR(50))
    ip_address = db.Column(db.String(50), default='')


class StaffAuditLog(TenantScoped, db.Model):
    """
    員工敏感操作稽核日誌表:追蹤金流操作、POS 收銀、重設 PIN、菜單刪除與點數異
    """
    __tablename__ = 'staff_audit_log'

    # 主鍵：稽核流水號 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 操作員工帳號 ID (INTEGER，可為空)
    staff_account_id = db.Column(db.Integer, nullable=True)

    # 操作者帳號名稱 (VARCHAR(100)，預設空字串)
    staff_username = db.Column(db.String(100), nullable=False, default='')

    # 操作者角色權限 (VARCHAR(30)，如：owner, manager, staff)
    staff_role = db.Column(db.String(30), nullable=False, default='staff')

    # 稽核動作類型代碼 (VARCHAR(60)，如：POS_CREATE_ORDER, CANCEL_ORDER, POS_SET_MEMBER_PIN)
    action_type = db.Column(db.String(60), nullable=False)

    # 異動關聯訂單 ID (INTEGER，可為空)
    target_order_id = db.Column(db.Integer, nullable=True)

    # 異動關聯會員 ID (INTEGER，可為空)
    target_user_id = db.Column(db.Integer, nullable=True)

    # 紅利點數增減值 (INTEGER，例如消費贈點為正數，折抵或手動扣點為負數)
    points_delta = db.Column(db.Integer, default=0)

    # 稽核詳細文字描述 (TEXT，包含金額、單號、折抵細節等)
    details = db.Column(db.Text, default='')

    # 操作客戶端 IP 位址 (VARCHAR(50))
    ip_address = db.Column(db.String(50), default='')

    # 紀錄建立時間 (DATETIME，預設當前系統時間)
    created_at = db.Column(db.DateTime, default=datetime.now)


class CustomerLog(TenantScoped, db.Model):
    """
    顧客點餐進出日誌表:記錄前台自助點餐機 (Kiosk) 或櫃檯 (POS) 顧客之進場時長與點購餐點摘要
    """
    __tablename__ = 'customer_log'

    # 主鍵：訪問日誌 ID (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 顧客進場型態 (VARCHAR(20)，例如：'會員', '訪客', 'POS會員', 'POS訪客')
    user_type = db.Column(db.String(20), nullable=False)

    # 顧客識別標示 (VARCHAR(100)，會員記錄姓名電話，訪客記錄桌號)
    identifier = db.Column(db.String(100), nullable=False)

    # 開始點餐/登入時間 (DATETIME，預設當前時間)
    login_at = db.Column(db.DateTime, default=datetime.now)

    # 結束點餐/登出時間 (DATETIME，結帳完成或關閉對話後寫入)
    logout_at = db.Column(db.DateTime, nullable=True)

    # 本次點餐內容文字摘要 (TEXT，儲存品項與份數快照)
    ordered_items = db.Column(db.Text, default='')

    # 顧客設備來源 IP 位址 (VARCHAR(50))
    ip_address = db.Column(db.String(50), default='')


class RewardSetting(TenantScoped, db.Model):
    """
    紅利點數系統設定表:定義各店家專屬的點數回饋門檻、折抵現金比率與單筆折抵上限。
    """
    __tablename__ = 'reward_setting'

    # 主鍵：設定編號 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 是否啟用紅利功能 (BOOLEAN，False 時關閉前台折抵與贈點)
    is_enabled = db.Column(db.Boolean, default=True)

    # 點數折抵現金比例 (INTEGER，代表多少點數可折抵 1 元 NTD，例如 1 代表 1點折$1)
    points_per_dollar = db.Column(db.Integer, default=1)

    # 單筆訂單點數折抵現金上限金額 (INTEGER，0 代表無折抵上限限制，單位 NTD)
    max_discount_per_order = db.Column(db.Integer, default=0)

    # 消費贈點門檻 (INTEGER，每消費多少元 NTD 贈送 1 點紅利，例如 100 代表滿百送一點)
    spend_per_point = db.Column(db.Integer, default=100)


class RewardRedemption(TenantScoped, db.Model):
    """
    紅利商品兌換憑證表:會員在紅利商城先行扣點兌換商品時建立憑證，結帳時核銷並綁定訂單
    """
    __tablename__ = 'reward_redemption'

    # 主鍵：兌換憑證識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 外鍵：兌換會員 ID (關聯 user.id，建立索引)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    # 外鍵：兌換餐點品項 ID (關聯 menu_item.id)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)

    # 本次兌換所消耗之紅利點數 (INTEGER，預設 0)
    points_cost = db.Column(db.Integer, nullable=False, default=0)

    # 憑證是否已於送單結帳時核銷使用 (BOOLEAN，預設 False)
    is_used = db.Column(db.Boolean, default=False, nullable=False)

    # 憑證是否已執行退點 (BOOLEAN，預設 False，退還點數後標記為 True 防止重複退款)
    is_refunded = db.Column(db.Boolean, default=False, nullable=False)

    # 最終綁定之訂單 ID (INTEGER，結帳完成後關聯 order.id)
    order_id = db.Column(db.Integer, nullable=True)

    # 憑證建立時間 (DATETIME，預設為當前系統時間)
    created_at = db.Column(db.DateTime, default=datetime.now)