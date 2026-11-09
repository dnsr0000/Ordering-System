"""Kiosk 顧客工作階段 (會員或訪客) 的開始與結束"""
import secrets
from datetime import datetime

from flask import g, request, session
from sqlalchemy import select

from app.errors import AppError
from app.extensions import db
from app.models.enums import VoucherStatus
from app.models.user import CustomerLog, RewardRedemption, User
from app.services.voucher_service import adjust_points, refund_open_vouchers, try_refund_voucher

# line_pending：LINE 授權後尚未綁定的帳號，登出時一併清除 (共用點餐機上不可留給下一位顧客)
# auth_factor：這次登入用的方式 (pin / face / line / line_kiosk)；人臉不是秘密因子、門市 (共用點餐機) 的 LINE 登入
#   可能沿用上一位顧客留在瀏覽器的 LINE 登入狀態 (line_kiosk)，這兩種敏感操作 (個資下載、會員條碼、解除 LINE) 要再輸入 PIN
# support_chats：這位顧客轉接過的真人客服對話憑證 (登出時一併清除，下一位顧客接續不了)
# user_uuid：登入會員的 uuid。SQLite 會重用被刪除會員的整數 id，取回會員時比對 uuid，避免工作階段變成下一位新註冊的會員
# voucher_uuids：這個工作階段在本機兌換的紅利商品憑證 (分店 / 線上端登出時只自動退還這些，見 _auto_refund_vouchers)
CUSTOMER_SESSION_KEYS = ('user_id', 'user_name', 'is_guest', 'customer_log_id', 'line_pending', 'auth_factor',
                         'support_chats', 'user_uuid', 'voucher_uuids')
WEAK_FACTORS = ('face', 'line_kiosk')
IDENTIFIER_MAX = 100   # CustomerLog.identifier 為 String(100)：PostgreSQL 超長會拋錯，分店上傳也會整批卡住
MAX_SESSION_VOUCHERS = 50


def _start(user_type, identifier):
    close_customer_session()
    log = CustomerLog(user_type=user_type, identifier=str(identifier or '')[:IDENTIFIER_MAX], login_at=datetime.now(),
                      ip_address=request.remote_addr or '')
    db.session.add(log)
    db.session.commit()
    session['customer_log_id'] = log.id


def remember_voucher(voucher):
    """兌換紅利商品後呼叫：記下這個工作階段在本機兌換的憑證 (登出 / 換人登入時自動退還)"""
    uuids = [u for u in (session.get('voucher_uuids') or []) if isinstance(u, str)]
    if voucher is not None and voucher.uuid and voucher.uuid not in uuids:
        session['voucher_uuids'] = (uuids + [voucher.uuid])[-MAX_SESSION_VOUCHERS:]


def _auto_refund_vouchers(tenant_id, user):
    """
    登入 / 登出時自動退還尚未結帳的兌換憑證點數。
    單機：退還該會員所有未使用的憑證 (只有這台主機會兌換 / 核銷)。
    分店 / 線上端：憑證會雙向同步，別台主機剛兌換、還沒結帳的憑證不可在這裡退點 (原主機之後結帳核銷，
    同步時 used 會蓋掉 refunded，退點的點數異動卻保留 → 點數憑空增加)，因此只退這個工作階段在本機兌換的憑證；
    其他未使用的憑證仍可在紅利商城「退回兌換」手動退還。
    """
    from app.sync import runtime as sync_runtime
    if sync_runtime.role() == sync_runtime.STANDALONE:
        refund_open_vouchers(tenant_id, user.id)
        return
    uuids = [u for u in (session.get('voucher_uuids') or []) if isinstance(u, str)][-MAX_SESSION_VOUCHERS:]
    if not uuids:
        return
    rows = db.session.execute(select(RewardRedemption.id, RewardRedemption.points_cost).where(
        RewardRedemption.tenant_id == tenant_id, RewardRedemption.user_id == user.id,
        RewardRedemption.status == VoucherStatus.OPEN, RewardRedemption.uuid.in_(uuids))).all()
    points = 0
    for vid, cost in rows:
        if try_refund_voucher(tenant_id, user.id, vid):
            points += int(cost or 0)
    if points:
        adjust_points(tenant_id, user.id, points, reason='voucher_refund')


def login_member(user, factor='pin'):
    """會員登入：先退回上次未結帳的兌換憑證點數，再開始新的進場紀錄。factor：pin / face / line / line_kiosk"""
    _auto_refund_vouchers(g.tenant_id, user)
    user.last_login_at = datetime.now()
    user.last_logout_at = None
    db.session.commit()
    _start('會員', f"{user.name} ({user.phone})")
    session.update(user_id=user.id, user_uuid=user.uuid, user_name=user.name, is_guest=False, auth_factor=factor)


def needs_pin_confirm():
    """
    這次登入只靠人臉 (可能誤認、且點餐機是共用裝置)，或是在門市 (共用點餐機) 以 LINE 登入
    (瀏覽器可能保留上一位顧客的 LINE 登入狀態)：敏感操作前要再輸入 PIN
    """
    return session.get('auth_factor') in WEAK_FACTORS


def require_confirmed(member, pin=None, label='此操作'):
    """
    敏感操作的身分確認：以 PIN 登入 / 線上端 LINE 登入的工作階段直接通過；人臉登入、門市 LINE 登入的工作階段需提供正確 PIN
    (與 phone_login 相同的鎖定規則)，通過後整個工作階段升級為已確認。沒有 PIN 時拋 403 (need_pin=True)。
    """
    if not needs_pin_confirm():
        return
    pin = str(pin or '').strip()
    if not pin:
        if session.get('auth_factor') == 'line_kiosk':
            raise AppError(f'{label}需輸入 PIN 碼確認身分 (您是在門市以 LINE 登入)', 403, need_pin=True)
        raise AppError(f'{label}需輸入 PIN 碼確認身分 (您是以人臉辨識登入)', 403, need_pin=True)
    from app.services import auth_throttle
    keys = auth_throttle.pin_keys(member.tenant_id, member.phone, request.remote_addr or '-')
    if auth_throttle.lock_remaining(keys.values()):
        raise AppError('PIN 碼錯誤次數過多，已暫時鎖定，請稍後再試', 429, need_pin=True)
    if not member.pin_hash:
        raise AppError('您的帳號尚未設定 PIN 碼，請洽櫃檯協助設定後再操作', 403, need_pin=True)
    # 先原子預扣裝置與帳號層級的額度再驗證：先檢查、驗證完才計數時，同時送出的一批請求都會通過鎖定檢查
    if auth_throttle.reserve_pin_attempt(keys):
        raise AppError('PIN 碼錯誤次數過多，已暫時鎖定，請稍後再試', 429, need_pin=True)
    if not member.check_pin(pin):
        auth_throttle.record_pin_failure(keys, ip_only=True)   # 裝置與帳號層級已預扣
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
    user = _session_user()
    if user:
        user.last_logout_at = datetime.now()
        _auto_refund_vouchers(user.tenant_id, user)
    db.session.commit()
    for key in CUSTOMER_SESSION_KEYS:
        session.pop(key, None)


def _session_user():
    """
    工作階段的會員；SQLite 會把被刪除會員的 id 指派給下一位新註冊的會員，所以以 uuid 確認是同一人 (不同時視為未登入)。
    升級前建立、沒有記 uuid 的工作階段：第一次取回時補記，之後同樣比對。
    """
    uid = session.get('user_id')
    user = db.session.get(User, uid) if uid else None
    if user is None:
        return None
    saved = session.get('user_uuid')
    if not saved:
        session['user_uuid'] = user.uuid
    elif saved != user.uuid:
        return None
    return user


def current_member():
    """目前登入的會員 (不屬於目前店家、或原會員已被刪除 (id 被重用) 時視為未登入)"""
    user = _session_user()
    return user if user and user.tenant_id == g.tenant_id else None
