"""訂位與現場候位 (門市本機資料，不同步)"""
import secrets
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


def new_public_token():
    return secrets.token_urlsafe(12)


class Reservation(TenantScoped, db.Model):
    """status：booked 已訂位 / seated 已入座 / cancelled 已取消 / no_show 未到"""
    __tablename__ = 'reservation'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    phone = db.Column(db.String(20), nullable=False, default='')
    party_size = db.Column(db.Integer, nullable=False, default=2)
    reserved_at = db.Column(db.DateTime, nullable=False, index=True)
    table_name = db.Column(db.String(50), nullable=False, default='')
    status = db.Column(db.String(12), nullable=False, default='booked', index=True)
    source = db.Column(db.String(12), nullable=False, default='staff')   # staff / online
    note = db.Column(db.String(200), nullable=False, default='')
    token = db.Column(db.String(32), nullable=False, unique=True, default=new_public_token)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    # ---- 訂金 (保證金)：線上訂位達設定人數時以綠界信用卡預付 ----
    # deposit_status：'' 不需訂金 / pending 待付款 / paid 已付 / refunded 已退 / forfeited 不退 (未到或逾時取消) /
    #                 applied 結帳折抵 / expired 逾時未付 (訂位自動取消) / refund_failed 退款失敗 (請到綠界後台退款)
    deposit_amount = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    deposit_status = db.Column(db.String(14), nullable=False, default='', server_default='', index=True)
    deposit_trade_no = db.Column(db.String(20), nullable=False, default='', server_default='', index=True)
    deposit_provider_no = db.Column(db.String(30), nullable=False, default='', server_default='')
    deposit_paid_at = db.Column(db.DateTime, nullable=True)
    deposit_note = db.Column(db.String(200), nullable=False, default='', server_default='')
    reminded_at = db.Column(db.DateTime, nullable=True)         # 已發送訂位提醒
    status_changed_at = db.Column(db.DateTime, nullable=True)

    DEPOSIT_LABELS = {'pending': '訂金待付款', 'paid': '訂金已付', 'refunded': '訂金已退', 'forfeited': '訂金不退',
                      'applied': '訂金已折抵', 'expired': '逾時未付', 'refund_failed': '訂金退款失敗'}

    @property
    def deposit_label(self):
        return self.DEPOSIT_LABELS.get(self.deposit_status, '')


class WaitlistEntry(TenantScoped, db.Model):
    """status：waiting 候位中 / called 已叫號 / seated 已入座 / cancelled 已取消"""
    __tablename__ = 'waitlist_entry'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'business_date', 'number', name='uq_waitlist_day_number'),)

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False)
    business_date = db.Column(db.Date, nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False, default='')
    phone = db.Column(db.String(20), nullable=False, default='')
    party_size = db.Column(db.Integer, nullable=False, default=2)
    status = db.Column(db.String(12), nullable=False, default='waiting', index=True)
    token = db.Column(db.String(32), nullable=False, unique=True, default=new_public_token)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    called_at = db.Column(db.DateTime, nullable=True)
    seated_at = db.Column(db.DateTime, nullable=True)
    table_name = db.Column(db.String(50), nullable=False, default='')

    @property
    def label(self):
        return f'W{self.number:03d}'
