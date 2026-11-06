"""
真人客服對話：顧客在 AI 客服對話窗按「轉接真人客服」後建立，店員在後台「客服對話」回覆。
門市本機資料 (不同步)：顧客連到哪一台主機，就在那一台的後台回覆。
"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped, new_public_token


class SupportChat(TenantScoped, db.Model):
    __tablename__ = 'support_chat'

    STATUSES = {'waiting': '待回覆', 'open': '已回覆', 'closed': '已結束'}   # waiting = 顧客在等店員回覆

    id = db.Column(db.Integer, primary_key=True)
    # 顧客端存取這個對話的憑證 (隨機、不可猜；只存在顧客的瀏覽器分頁)
    token = db.Column(db.String(40), nullable=False, unique=True, index=True, default=new_public_token)
    status = db.Column(db.String(10), nullable=False, default='waiting', index=True)
    customer_label = db.Column(db.String(60), nullable=False, default='')   # 會員姓名 / 取餐號 / 「訪客」
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    page = db.Column(db.String(20), nullable=False, default='')   # menu / track：顧客從哪一頁轉接
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    last_customer_at = db.Column(db.DateTime, nullable=True)
    last_staff_at = db.Column(db.DateTime, nullable=True)
    staff_seen_id = db.Column(db.Integer, nullable=False, default=0)   # 店員看過的最後一則訊息 id (未讀數用)
    closed_at = db.Column(db.DateTime, nullable=True)
    closed_by = db.Column(db.String(120), nullable=False, default='')

    messages = db.relationship('SupportMessage', backref='chat', lazy='selectin', order_by='SupportMessage.id',
                               cascade='all, delete-orphan')

    @property
    def status_label(self):
        return self.STATUSES.get(self.status, self.status)


class SupportMessage(TenantScoped, db.Model):
    __tablename__ = 'support_message'

    SENDERS = ('customer', 'bot', 'staff', 'system')

    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('support_chat.id', ondelete='CASCADE'), nullable=False, index=True)
    sender = db.Column(db.String(10), nullable=False)
    staff_name = db.Column(db.String(120), nullable=False, default='')
    text = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
