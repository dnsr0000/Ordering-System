"""Kiosk 顧客工作階段 (會員或訪客) 的開始與結束"""
import secrets
from datetime import datetime

from flask import g, request, session

from app.errors import AppError
from app.extensions import db
from app.models.user import CustomerLog, User
from app.services.voucher_service import refund_open_vouchers

# line_pending：LINE 授權後尚未綁定的帳號，登出時一併清除 (共用點餐機上不可留給下一位顧客)
# auth_factor：這次登入用的方式 (pin / face / line)；人臉不是秘密因子，敏感操作 (個資下載、會員條碼、解除 LINE) 要再輸入 PIN
CUSTOMER_SESSION_KEYS = ('user_id', 'user_name', 'is_guest', 'customer_log_id', 'line_pending', 'auth_factor')
WEAK_FACTORS = ('face',)


def _start(user_type, identifier):
    close_customer_session()
    log = CustomerLog(user_type=user_type, identifier=identifier, login_at=datetime.now(),
                      ip_address=request.remote_addr or '')
    db.session.add(log)
    db.session.commit()
    session['customer_log_id'] = log.id


def login_member(user, factor='pin'):
    """會員登入：先退回上次未結帳的兌換憑證點數，再開始新的進場紀錄。factor：pin / face / line"""
    refund_open_vouchers(g.tenant_id, user.id)
    user.last_login_at = datetime.now()
    user.last_logout_at = None
    db.session.commit()
    _start('會員', f"{user.name} ({user.phone})")
    session.update(user_id=user.id, user_name=user.name, is_guest=False, auth_factor=factor)


def needs_pin_confirm():
    """這次登入只靠人臉 (可能誤認、且點餐機是共用裝置)：敏感操作前要再輸入 PIN"""
    return session.get('auth_factor') in WEAK_FACTORS


def require_confirmed(member, pin=None, label='此操作'):
    """
    敏感操作的身分確認：以 PIN 登入 / LINE 登入的工作階段直接通過；人臉登入的工作階段需提供正確 PIN
    (與 phone_login 相同的鎖定規則)，通過後整個工作階段升級為已確認。沒有 PIN 時拋 403 (need_pin=True)。
    """
    if not needs_pin_confirm():
        return
    pin = str(pin or '').strip()
    if not pin:
        raise AppError(f'{label}需輸入 PIN 碼確認身分 (您是以人臉辨識登入)', 403, need_pin=True)
    from app.services import auth_throttle
    keys = auth_throttle.pin_keys(member.tenant_id, member.phone, request.remote_addr or '-')
    if auth_throttle.lock_remaining(keys.values()):
        raise AppError('PIN 碼錯誤次數過多，已暫時鎖定，請稍後再試', 429, need_pin=True)
    if not member.pin_hash:
        raise AppError('您的帳號尚未設定 PIN 碼，請洽櫃檯協助設定後再操作', 403, need_pin=True)
    if not member.check_pin(pin):
        auth_throttle.record_pin_failure(keys)
        raise AppError('PIN 碼不正確', 403, need_pin=True)
    auth_throttle.clear_keys(keys['device'], keys['account'])
    session['auth_factor'] = 'pin'


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
