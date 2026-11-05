import secrets
import uuid
from datetime import datetime

from app.extensions import db


def new_uuid():
    return str(uuid.uuid4())


def new_public_token():
    """公開網址中的代碼 (桌位 QR code、訂位 / 候位查詢連結)：隨機產生、無法猜測；重新產生後舊連結立即失效"""
    return secrets.token_urlsafe(12)


class SyncMixin:
    """
    同步用欄位：本機 (SQLite) 與線上 (PostgreSQL) 之間交換資料時，整數 id 會撞號，
    因此每一列都帶一個全域唯一的 uuid 作為跨資料庫的識別，並以 updated_at 判斷新舊。
    """
    uuid = db.Column(db.String(36), nullable=False, unique=True, index=True, default=new_uuid)
    updated_at = db.Column(db.DateTime, nullable=True, default=datetime.now, onupdate=datetime.now)
    # 本資料庫的變更序號：每次提交時由 app/sync/tracking.py 依提交順序遞增；同步只傳送序號大於游標的列
    sync_rev = db.Column(db.BigInteger, nullable=False, default=0, server_default='0', index=True)


class TenantScoped(SyncMixin):
    """
    多租戶隔離 Mixin：所有歸屬單一店家的資料表都繼承此類別，
    查詢與寫入由 app.tenant_context 依目前請求的店家自動過濾。
    """
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)


class CreatedAtMixin:
    """建立時間 (本機時間)"""
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class KindLabelMixin:
    """kind 欄位的中文名稱：子類別以 KINDS = {代碼: 名稱} 指定"""
    KINDS = {}

    @property
    def kind_label(self):
        return self.KINDS.get(self.kind, self.kind)


class LastSeenMixin:
    """裝置 / 分店主機的最後連線時間與線上判斷"""
    last_seen_at = db.Column(db.DateTime, nullable=True)

    def is_online(self, seconds=180, now=None):
        """最近 seconds 秒內有連線"""
        now = now or datetime.now()
        return bool(self.last_seen_at) and (now - self.last_seen_at).total_seconds() <= seconds
