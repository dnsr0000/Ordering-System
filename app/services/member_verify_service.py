"""
POS 動用會員資產 (儲值金付款、點數折抵、寄杯兌換) 前的會員身分驗證。

只憑手機號碼查到會員就能扣款，知道別人手機的人 (或店員自己) 就能花掉別人的儲值金；因此：
- 會員出示手機上的動態條碼 (member_qr_service) 被掃描後，這個收銀工作階段 15 分鐘內視為已驗證；
- 或由會員當面輸入 PIN (與點餐機登入相同的鎖定規則，防猜測)；
- 都不行時 (會員沒帶手機、忘記 PIN) 由具備「免驗證會員身分」權限的主管核准 (approval_service；店長以上預設有)。
後台「會員經營」可關閉 (小店信任櫃檯的模式)。
"""
import time

from flask import request, session

from app.errors import AppError
from app.services import approval_service, auth_throttle, settings_service
from app.services.audit import record_staff_audit
from app.utils import safe_int

SESSION_KEY = 'pos_member_verified'
VERIFIED_TTL = 15 * 60
MAX_REMEMBERED = 20


def enabled(tenant_id=None):
    return bool(settings_service.get('member', tenant_id).get('pos_verify', True))


def mark_verified(user_id, now=None):
    """記下這個收銀工作階段已驗證的會員 (掃條碼、PIN 正確或主管核准後)"""
    now = now or time.time()
    rows = {k: v for k, v in (session.get(SESSION_KEY) or {}).items() if now - float(v) < VERIFIED_TTL}
    rows[str(user_id)] = now
    if len(rows) > MAX_REMEMBERED:
        rows = dict(sorted(rows.items(), key=lambda kv: kv[1])[-MAX_REMEMBERED:])
    session[SESSION_KEY] = rows


def is_verified(user_id, now=None):
    now = now or time.time()
    at = (session.get(SESSION_KEY) or {}).get(str(user_id))
    return at is not None and now - float(at) < VERIFIED_TTL


def uses_member_assets(data):
    """結帳請求是否動用會員資產：儲值金 (含混合付款的儲值金)、點數折抵、寄杯兌換"""
    if str(data.get('payment_method') or '') == '儲值金':
        return True
    if safe_int(data.get('use_points'), 0) > 0:
        return True
    tenders = ((data.get('payment_info') or {}).get('tenders') if isinstance(data.get('payment_info'), dict) else None) or []
    if any(isinstance(t, dict) and str(t.get('method') or '') == '儲值金' for t in tenders):
        return True
    return any(isinstance(i, dict) and safe_int(i.get('pass_id'), 0) > 0 for i in (data.get('items') or []))


def require_verified(user, data, label='動用會員儲值金 / 點數 / 寄杯'):
    """
    未驗證時依請求內容驗證：member_pin (會員當面輸入) 或主管帳密 (approver_username / approver_password)。
    驗證通過後記住 15 分鐘；都沒有時拋出 403 (need_member_pin=True，POS 據此跳出會員驗證視窗)。
    """
    if not enabled(user.tenant_id) or is_verified(user.id):
        return 'session'
    pin = str(data.get('member_pin') or '').strip()
    if pin:
        keys = auth_throttle.pin_keys(user.tenant_id, user.phone, request.remote_addr or '-')
        if auth_throttle.lock_remaining(keys.values()):
            raise AppError('此會員 PIN 碼錯誤次數過多，已暫時鎖定；請改由主管核准或協助重設 PIN', 429, need_member_pin=True,
                           member_name=user.name, has_pin=bool(user.pin_hash))
        if not user.pin_hash:
            raise AppError('此會員尚未設定 PIN：請先在會員區協助設定，或改由主管核准', 403, need_member_pin=True,
                           member_name=user.name, has_pin=False)
        if not user.check_pin(pin):
            auth_throttle.record_pin_failure(keys)
            raise AppError('會員 PIN 碼不正確，請重新輸入', 403, need_member_pin=True, member_name=user.name, has_pin=True)
        auth_throttle.clear_keys(keys['device'], keys['account'])
        mark_verified(user.id)
        return 'pin'
    if (data.get('approver_username') or '').strip() or data.get('member_verify_override'):
        approver = approval_service.require_approval('member_verify', data, f'未驗證會員身分即{label}')
        record_staff_audit('MEMBER_VERIFY_OVERRIDE', target_user_id=user.id,
                           details=f'主管 {approver} 核准未驗證身分即{label}：會員 {user.name} ({user.phone})')
        mark_verified(user.id)
        return 'approval'
    raise AppError(f'{label}需先驗證會員身分：請掃描會員手機上的動態條碼，或請會員輸入 PIN 碼', 403,
                   need_member_pin=True, member_name=user.name, has_pin=bool(user.pin_hash))
