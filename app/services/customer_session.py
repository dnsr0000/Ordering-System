"""Kiosk 顧客工作階段 (會員或訪客) 的開始與結束"""
import secrets
from datetime import datetime

from flask import g, request, session

from app.extensions import db
from app.models.user import CustomerLog, User
from app.services.voucher_service import refund_open_vouchers

CUSTOMER_SESSION_KEYS = ('user_id', 'user_name', 'is_guest', 'customer_log_id')


def _start(user_type, identifier):
    close_customer_session()
    log = CustomerLog(user_type=user_type, identifier=identifier, login_at=datetime.now(),
                      ip_address=request.remote_addr or '')
    db.session.add(log)
    db.session.commit()
    session['customer_log_id'] = log.id


def login_member(user):
    """會員登入：先退回上次未結帳的兌換憑證點數，再開始新的進場紀錄"""
    refund_open_vouchers(g.tenant_id, user.id)
    user.last_login_at = datetime.now()
    user.last_logout_at = None
    db.session.commit()
    _start('會員', f"{user.name} ({user.phone})")
    session.update(user_id=user.id, user_name=user.name, is_guest=False)


def login_guest():
    guest_name = f"訪客-{secrets.randbelow(9000) + 1000}"
    _start('訪客', guest_name)
    session.update(user_id=None, user_name=guest_name, is_guest=True)


def close_customer_session():
    """結束進場紀錄；會員登出時退回尚未結帳使用的兌換憑證點數"""
    log_id = session.get('customer_log_id')
    log = db.session.get(CustomerLog, log_id) if log_id else None
    if log and not log.logout_at:
        log.logout_at = datetime.now()
    user = db.session.get(User, session['user_id']) if session.get('user_id') else None
    if user:
        user.last_logout_at = datetime.now()
        refund_open_vouchers(user.tenant_id, user.id)
    db.session.commit()
    for key in CUSTOMER_SESSION_KEYS:
        session.pop(key, None)


def current_member():
    """目前登入的會員 (不屬於目前店家時視為未登入)"""
    uid = session.get('user_id')
    user = db.session.get(User, uid) if uid else None
    return user if user and user.tenant_id == g.tenant_id else None
