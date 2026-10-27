from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class StoreSetting(TenantScoped, db.Model):
    """
    店家設定 (每店每個 key 一筆，value 為 JSON 字串)：電子發票、LINE Pay、刷卡機、服務費、廚房、通知、
    外部服務金鑰等，都由後台「系統設定」頁面填寫 (欄位定義見 app/services/settings_service.py)。
    門市本機資料，不同步 (每台主機各自設定；分店可能是不同的營業人、印表機位址也不同)。
    不含金鑰的營運規則另由線上端以 HqSetting 下發 (分店沒有自己的設定時使用總部的值)。
    """
    __tablename__ = 'store_setting'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'key', name='uq_store_setting_tenant_key'),)

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), nullable=False)
    value = db.Column(db.Text, nullable=False, default='{}')
    changed_by = db.Column(db.String(120), nullable=False, default='')
    changed_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class HqSetting(TenantScoped, db.Model):
    """
    總部 (線上端) 下發給所有分店的設定：線上端在後台儲存「不含金鑰」的設定區塊時同步寫一份到這裡，
    同步方向只有線上端 → 分店 (分店上傳的會被拒絕)。分店沒有自己的 StoreSetting 時就使用這裡的值。
    """
    __tablename__ = 'hq_setting'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'key', name='uq_hq_setting_tenant_key'),)

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), nullable=False)
    value = db.Column(db.Text, nullable=False, default='{}')
    changed_by = db.Column(db.String(120), nullable=False, default='')
    changed_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
