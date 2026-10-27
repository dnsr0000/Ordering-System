"""
會員分群與自動行銷 (線上端 / 單機執行，不同步；發出的會員券照常同步到各分店)。
"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped

CAMPAIGN_KINDS = {'winback': '久未消費自動發券', 'broadcast': '排程推播 (分群)'}


class MarketingCampaign(TenantScoped, db.Model):
    """
    winback：每天檢查「最後一次消費已超過 inactive_days 天」的會員，發券 / 推播 (同一位會員 cooldown_days 天內只發一次)。
    broadcast：在 send_at 對某個分群發一次 (發完改為 done)。
    status：active 啟用 / paused 暫停 / running 執行中 (防止同時執行兩次) / done 已完成 (broadcast)
    """
    __tablename__ = 'marketing_campaign'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    kind = db.Column(db.String(12), nullable=False, default='winback')
    segment = db.Column(db.String(20), nullable=False, default='all')
    inactive_days = db.Column(db.Integer, nullable=False, default=60)
    cooldown_days = db.Column(db.Integer, nullable=False, default=90)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id', ondelete='SET NULL'), nullable=True)
    coupon_valid_days = db.Column(db.Integer, nullable=False, default=14)
    message = db.Column(db.String(500), nullable=False, default='')
    send_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(10), nullable=False, default='active', index=True)
    created_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    last_run_at = db.Column(db.DateTime, nullable=True)
    sent_count = db.Column(db.Integer, nullable=False, default=0)

    coupon = db.relationship('Coupon')

    @property
    def kind_label(self):
        return CAMPAIGN_KINDS.get(self.kind, self.kind)


class CampaignSend(TenantScoped, db.Model):
    """每位會員收到某個活動的紀錄 (成效：之後 30 天內是否回來消費)"""
    __tablename__ = 'campaign_send'
    __table_args__ = (db.Index('ix_campaign_send_campaign_user', 'campaign_id', 'user_id'),)

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('marketing_campaign.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    channel = db.Column(db.String(10), nullable=False, default='')      # line / coupon (只發券，沒有 LINE)
    batch_id = db.Column(db.String(32), nullable=False, default='')     # 發券批次 (會員券使用狀況)
