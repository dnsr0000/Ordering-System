"""
線上 (hub) 與分店 (node) 同步用的資料表。設計說明見 SYNC.txt。

所有角色都建立全部資料表 (結構一致、遷移單純)，但各自只使用其中一部分：
- 共用：sync_clock (變更序號)、sync_tombstone (刪除紀錄)、point_ledger (點數異動帳)
- 線上端：sync_node (分店與權杖)、node_stock (分店回報的庫存)、sync_alias (重複資料的合併對照)
- 分店端：sync_state (游標與狀態)、sync_conflict (被線上端拒絕的資料)
"""
import hashlib
import secrets
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped, new_uuid


class SyncClock(db.Model):
    """單列計數器。提交時以條件式 UPDATE 遞增並鎖住此列直到提交完成，因此變更序號與提交順序一致。"""
    __tablename__ = 'sync_clock'

    id = db.Column(db.Integer, primary_key=True)
    rev = db.Column(db.BigInteger, nullable=False, default=0)


class SyncTombstone(TenantScoped, db.Model):
    """被刪除的資料列 (資料表名稱 + uuid)，同步給其他資料庫套用同樣的刪除"""
    __tablename__ = 'sync_tombstone'

    id = db.Column(db.Integer, primary_key=True)
    table_name = db.Column(db.String(50), nullable=False)
    row_uuid = db.Column(db.String(36), nullable=False, index=True)
    deleted_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class PointLedger(TenantScoped, db.Model):
    """
    會員點數異動帳 (只新增、不修改)。user.points 是本帳的合計快取。
    各分店離線時各自記帳，同步時合併所有異動再重算合計，不會互相覆蓋。
    """
    __tablename__ = 'point_ledger'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(30), nullable=False, default='')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    user = db.relationship('User', backref=db.backref('point_entries', lazy='dynamic', passive_deletes=True))


def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class SyncNode(TenantScoped, db.Model):
    """(線上端) 分店：每台本機伺服器一個，以權杖驗證身分"""
    __tablename__ = 'sync_node'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    accepts_online_orders = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(50), default='')

    tenant = db.relationship('Tenant')

    def is_online(self, offline_after_seconds):
        """最近是否有連線 (分店長輪詢約每 20 秒連線一次)"""
        return bool(self.last_seen_at) and (datetime.now() - self.last_seen_at).total_seconds() <= offline_after_seconds

    def issue_token(self):
        """產生新權杖 (只回傳這一次，資料庫只存雜湊)"""
        token = 'kn_' + secrets.token_urlsafe(32)
        self.token_hash = hash_token(token)
        return token


class NodeStock(db.Model):
    """(線上端) 分店回報的庫存快照，線上點餐依此檢查與預扣庫存"""
    __tablename__ = 'node_stock'

    node_id = db.Column(db.Integer, db.ForeignKey('sync_node.id', ondelete='CASCADE'), primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), primary_key=True)
    stock = db.Column(db.Integer, nullable=False, default=0)
    is_sold_out = db.Column(db.Boolean, nullable=False, default=False)
    reported_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class SyncAlias(db.Model):
    """(線上端) 分店送來的資料與既有資料重複 (例如同一支手機在兩家分店各註冊一次) 時，記錄合併到哪一列"""
    __tablename__ = 'sync_alias'

    table_name = db.Column(db.String(50), primary_key=True)
    alias_uuid = db.Column(db.String(36), primary_key=True)
    canonical_uuid = db.Column(db.String(36), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class SyncState(db.Model):
    """(分店端) 同步狀態鍵值：node_uuid、游標、最後成功時間、最後錯誤等"""
    __tablename__ = 'sync_state'

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='')
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class SyncConflict(db.Model):
    """(分店端) 線上端拒收的資料 (例如員工帳號名稱已被其他店家使用)，顯示在同步狀態頁"""
    __tablename__ = 'sync_conflict'

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), nullable=False, default=new_uuid)
    table_name = db.Column(db.String(50), nullable=False)
    row_uuid = db.Column(db.String(36), nullable=False)
    reason = db.Column(db.String(300), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
