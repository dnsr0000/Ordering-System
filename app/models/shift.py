"""收銀交班：開班零用金、現金盤點、帳差 (門市本機資料，不同步)"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class CashShift(TenantScoped, db.Model):
    __tablename__ = 'cash_shift'

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(10), nullable=False, default='open', index=True)   # open / closed
    opened_by = db.Column(db.String(120), nullable=False, default='')
    opened_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    opening_cash = db.Column(db.Integer, nullable=False, default=0)
    closed_by = db.Column(db.String(120), nullable=False, default='')
    closed_at = db.Column(db.DateTime, nullable=True)
    expected_cash = db.Column(db.Integer, nullable=False, default=0)
    counted_cash = db.Column(db.Integer, nullable=False, default=0)
    difference = db.Column(db.Integer, nullable=False, default=0)
    cash_detail = db.Column(db.Text, nullable=False, default='')   # 盤點面額明細 (JSON)
    note = db.Column(db.String(300), nullable=False, default='')
    report = db.Column(db.Text, nullable=False, default='')         # 關班時的報表快照 (JSON)
