"""
員工權限與主管核准。

角色有預設權限 (enums.ROLE_DEFAULT_PERMISSIONS)，負責人可在員工帳號頁另外授權。
沒有權限的員工執行改價、整單折扣、作廢、退款時，需由有權限的主管在同一個畫面輸入帳號密碼核准：
請求中帶 approver_username / approver_password，核准人寫入稽核紀錄與訂單。
"""
from functools import wraps

from flask import g, request
from flask_login import current_user

from app.errors import AppError
from app.models.enums import PERMISSIONS
from app.models.tenant import StaffAccount
from app.services import auth_throttle


def has_permission(perm, staff=None):
    staff = staff if staff is not None else current_user
    return bool(getattr(staff, 'is_authenticated', False)) and staff.has_permission(perm)


def permission_required(perm):
    """需登入且具備權限 (角色預設或個別授權)"""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                from app.extensions import login_manager
                return login_manager.unauthorized()
            if not current_user.has_permission(perm):
                raise AppError(f'權限不足：需要「{PERMISSIONS.get(perm, perm)}」權限，請洽負責人授權！', 403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def require_approval(perm, data, action_label):
    """
    目前員工有權限 → 回傳自己的帳號；否則驗證 data 內的主管帳密 (同店、可登入、具備該權限)，回傳核准人帳號。
    未提供或錯誤時拋出 403 (need_approval=True，前端據此跳出核准視窗)。
    """
    if has_permission(perm):
        return current_user.username
    username = str((data or {}).get('approver_username') or '').strip()
    password = str((data or {}).get('approver_password') or '')
    need = AppError(f'「{action_label}」需要主管核准：請主管輸入帳號密碼', 403, need_approval=True,
                    permission=perm)
    if not username or not password:
        raise need
    keys = auth_throttle.staff_keys(username, request.remote_addr or '-')
    if auth_throttle.staff_lock_remaining(keys):
        raise AppError('主管帳號密碼錯誤次數過多，已暫時鎖定，請稍後再試', 429, need_approval=True)
    approver = StaffAccount.query.filter_by(username=username).first()
    if approver is None:
        auth_throttle.spend_hash_time(password)
    if (approver is None or approver.tenant_id != g.tenant_id or not approver.can_login
            or not approver.check_password(password)):
        auth_throttle.record_staff_failure(keys)
        raise AppError('核准失敗：主管帳號或密碼錯誤', 403, need_approval=True, permission=perm)
    if not approver.has_permission(perm):
        raise AppError(f'核准失敗：{approver.username} 沒有「{PERMISSIONS.get(perm, perm)}」權限', 403,
                       need_approval=True, permission=perm)
    auth_throttle.clear_keys(keys['device'], keys['account'])
    return approver.username
