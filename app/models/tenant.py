from datetime import datetime
from werkzeug.security import check_password_hash, generate_password_hash
from app.extensions import db


class Tenant(db.Model):
    """
    店家租戶資料表:儲存系統中各加盟店或獨立分店之主體資訊。
    """
    __tablename__ = 'tenant'

    # 主鍵：店家唯一識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 店家名稱 (VARCHAR(120)，必填)
    name = db.Column(db.String(120), nullable=False)

    # 店家專屬網址代號 (VARCHAR(80)，唯一值，建立索引，用於 /shop/<slug> 路由辨識)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)

    # 店家啟用狀態 (BOOLEAN，預設 True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # 店家建立時間 (DATETIME，預設為當前系統時間)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class StaffAccount(db.Model):
    """
    員工與後台管理員帳號資料表:記錄各店家所屬員工之登入憑證與 RBAC 角色權限。
    """
    __tablename__ = 'staff_account'

    # 主鍵：員工帳號流水編號 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 外鍵：所屬店家 ID (關聯 tenant.id，建立索引)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)

    # 員工登入帳號名稱 (VARCHAR(120)，全系統唯一，建立索引)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)

    # 經 Werkzeug 雜湊加密之密碼字串 (VARCHAR(255)，不可為空)
    password_hash = db.Column(db.String(255), nullable=False)

    # 權限角色 (VARCHAR(30)，預設 'staff'；可選：owner, manager, staff, kitchen)
    role = db.Column(db.String(30), nullable=False, default='staff')

    # 帳號啟用狀態 (BOOLEAN，預設 True；停用後將禁止登入)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # 帳號建立時間 (DATETIME，預設當前系統時間)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    # 軟刪除時間 (DATETIME，可為空)：非空代表帳號已被負責人刪除。
    # 列不會真的移除，稽核紀錄 (StaffAuditLog.staff_account_id) 才能永遠對應到當時的帳號；id 也不會被新帳號重用。
    deleted_at = db.Column(db.DateTime, nullable=True)

    # 關聯：所屬店家實體物件
    tenant = db.relationship('Tenant', backref=db.backref('staff_accounts', lazy=True))

    def set_password(self, password):
        """將明文密碼進行 Hash 雜湊處理後寫入資料庫"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """驗證明文密碼是否與資料庫內的雜湊值相符"""
        return check_password_hash(self.password_hash, password)


class TenantScoped:
    """
    多租戶隔離抽象 Mixin
    所有歸屬單一店家之資料表皆繼承此類別，透過 tenant_id 達成 ORM 層級資料自動隔離
    """
    # 外鍵：所屬店家 ID (關聯 tenant.id，建立索引)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)
