"""員工打卡與排班 (門市本機資料，不同步)"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class TimeClock(TenantScoped, db.Model):
    __tablename__ = 'time_clock'

    id = db.Column(db.Integer, primary_key=True)
    staff_account_id = db.Column(db.Integer, db.ForeignKey('staff_account.id', ondelete='CASCADE'), nullable=False, index=True)
    clock_in = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    clock_out = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(200), nullable=False, default='')
    edited_by = db.Column(db.String(120), nullable=False, default='')

    staff = db.relationship('StaffAccount')

    @property
    def hours(self):
        if not self.clock_out:
            return 0.0
        return max(0.0, (self.clock_out - self.clock_in).total_seconds() / 3600)


class WorkSchedule(TenantScoped, db.Model):
    __tablename__ = 'work_schedule'

    id = db.Column(db.Integer, primary_key=True)
    staff_account_id = db.Column(db.Integer, db.ForeignKey('staff_account.id', ondelete='CASCADE'), nullable=False, index=True)
    work_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    note = db.Column(db.String(100), nullable=False, default='')

    staff = db.relationship('StaffAccount')
