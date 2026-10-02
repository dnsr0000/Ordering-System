from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


class Tenant(db.Model):
    __tablename__ = 'tenant'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class StaffAccount(db.Model):
    __tablename__ = 'staff_account'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default='staff')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    tenant = db.relationship('Tenant', backref=db.backref('staff_accounts', lazy=True))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class TenantScoped:
    """Mixin for rows that belong to exactly one shop tenant."""

    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False, index=True)
