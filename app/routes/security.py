"""員工帳號安全：登入第二步 (兩步驟驗證)、「帳號安全」頁 (設定驗證 App、備用碼、登入裝置管理)"""
import time

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app.errors import AppError, wants_json
from app.extensions import db, ip_key, limiter
from app.models.tenant import StaffAccount
from app.services import auth_throttle, security_service, table_service
from app.services.audit import record_staff_audit
from app.sync import runtime as sync_runtime
from app.utils import safe_id

security_bp = Blueprint('security', __name__)
SETUP_KEY = 'totp_setup'
REBIND_KEY = 'totp_rebind'   # 已啟用的帳號通過密碼 + 驗證碼 / 備用碼確認，正在重新綁定驗證 App
# 會改變 totp_* 欄位的操作：分店上的負責人帳號不可執行 (線上端對負責人的 totp_* 異動整列拒收)
TOTP_CHANGE_ACTIONS = {'start_2fa', 'rebind_2fa', 'confirm_2fa', 'regen_backup', 'disable_2fa'}
OWNER_ON_NODE_MESSAGE = '❌ 負責人帳號的兩步驟驗證請到線上端設定 (分店的修改不會同步到線上端)'
# 尚未啟用但被要求兩步驟驗證時，仍可使用的頁面
ALLOWED_WHEN_SETUP_REQUIRED = {'security.account', 'security.two_factor', 'admin.admin_logout', 'static',
                               'health.healthz'}


def owner_on_node(staff):
    """分店上的負責人帳號：兩步驟驗證只能在線上端設定 (比照員工帳號頁對負責人帳號的分店限制)"""
    return getattr(staff, 'role', None) == 'owner' and sync_runtime.is_node()


@security_bp.before_app_request
def enforce_required_2fa():
    """系統設定要求這個角色必須啟用兩步驟驗證：未啟用前只能進入「帳號安全」頁"""
    if request.endpoint in ALLOWED_WHEN_SETUP_REQUIRED or request.endpoint is None:
        return None
    if not getattr(current_user, 'is_authenticated', False) or not isinstance(current_user, StaffAccount):
        return None
    if current_user.totp_enabled or not security_service.required_for(current_user, getattr(g, 'tenant_id', None)):
        return None
    if owner_on_node(current_user):   # 分店無法替負責人啟用 (會被線上端拒收)，不強制導去設定，否則負責人會卡住
        return None
    message = '本店要求您的帳號啟用兩步驟驗證，請先在「帳號安全」頁完成設定'
    if wants_json():
        return jsonify({'success': False, 'message': message, 'need_2fa_setup': True}), 403
    flash(f'{message}', 'warning')
    return redirect(url_for('security.account'))


# ---------------------------------------------------------------------------
# 登入第二步
# ---------------------------------------------------------------------------
@security_bp.route('/admin/2fa', methods=['GET', 'POST'], endpoint='two_factor')
@limiter.limit('20/minute', methods=['POST'], key_func=ip_key)
def two_factor():
    pending = session.get(security_service.PENDING_KEY) or {}
    staff = db.session.get(StaffAccount, int(pending.get('id') or 0)) if pending else None
    expired = not pending or time.time() - float(pending.get('at') or 0) > security_service.PENDING_TTL
    if staff is None or expired or not staff.can_login or pending.get('v') != staff.session_version:
        session.pop(security_service.PENDING_KEY, None)
        flash('請重新輸入帳號密碼登入', 'warning')
        return redirect(url_for('admin.admin_dashboard'))
    if request.method == 'GET':
        return render_template('admin_2fa.html', username=staff.username)
    keys = auth_throttle.staff_keys(staff.username, request.remote_addr or '-')
    remaining = auth_throttle.staff_lock_remaining(keys)
    if remaining:
        session.pop(security_service.PENDING_KEY, None)
        minutes = max(1, int((remaining.total_seconds() + 59) // 60))
        flash(f'驗證碼錯誤次數過多，此帳號已暫時鎖定，請於 {minutes} 分鐘後再試。', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    method = security_service.verify_second_factor(staff, request.form.get('code'))
    if method is None:
        auth_throttle.record_staff_failure(keys)
        flash('驗證碼不正確 (或已使用過)，請輸入驗證 App 上目前的 6 位數，或一組備用碼', 'danger')
        return render_template('admin_2fa.html', username=staff.username), 401
    auth_throttle.clear_keys(keys['device'], keys['account'])
    session.pop(security_service.PENDING_KEY, None)
    if method == 'backup':
        flash(f'已使用一組備用碼登入，剩下 {security_service.remaining_backup_codes(staff)} 組；'
              f'手機遺失請到「帳號安全」重新設定驗證 App', 'warning')
    from app.routes.admin import complete_staff_login
    return complete_staff_login(staff)


# ---------------------------------------------------------------------------
# 帳號安全頁
# ---------------------------------------------------------------------------
def _confirm_password_and_code(staff, password=True):
    """
    停用 / 重新設定驗證 App / 重新產生備用碼前確認密碼 (password=True 時) 與第二步驗證 (驗證碼或備用碼)。
    與登入頁共用失敗計數與鎖定，且密碼錯與驗證碼錯回同一則訊息：拿到已登入的工作階段也無法藉此無限次猜密碼或驗證碼。
    """
    keys = _unlocked_staff_keys(staff)
    if (password and not staff.check_password(request.form.get('password') or '')) \
            or security_service.verify_second_factor(staff, request.form.get('code')) is None:
        auth_throttle.record_staff_failure(keys)
        raise AppError('目前的密碼不正確，或驗證碼不正確 (或已使用過)' if password else '驗證碼不正確 (或已使用過)')
    auth_throttle.clear_keys(keys['device'], keys['account'])


def _unlocked_staff_keys(staff):
    """與登入頁共用的失敗計數鍵；鎖定期間直接拒絕 (連正確的密碼 / 驗證碼也不放行)"""
    keys = auth_throttle.staff_keys(staff.username, request.remote_addr or '-')
    remaining = auth_throttle.staff_lock_remaining(keys)
    if remaining:
        minutes = max(1, int((remaining.total_seconds() + 59) // 60))
        raise AppError(f'錯誤次數過多，此帳號已暫時鎖定，請於 {minutes} 分鐘後再試', 429)
    return keys


@security_bp.route('/admin/security', methods=['GET', 'POST'], endpoint='account')
@login_required
@limiter.limit('20/minute', methods=['POST'], key_func=ip_key)
def account():
    staff = current_user
    new_codes = None
    if request.method == 'POST':
        action = request.form.get('action')
        if action in TOTP_CHANGE_ACTIONS and owner_on_node(staff):
            raise AppError(OWNER_ON_NODE_MESSAGE, 403)
        if action == 'start_2fa':
            if staff.totp_enabled:
                raise AppError('已啟用兩步驟驗證')
            session[SETUP_KEY] = security_service.new_secret()
        elif action == 'rebind_2fa':
            # 手機遺失 / 換手機：以密碼 + 驗證碼或備用碼確認後重新綁定驗證 App；新的 App 確認前舊的設定仍然有效
            if not staff.totp_enabled:
                raise AppError('尚未啟用兩步驟驗證')
            _confirm_password_and_code(staff)
            session[SETUP_KEY], session[REBIND_KEY] = security_service.new_secret(), True
            db.session.commit()   # 用掉的驗證碼 / 備用碼
            flash('請用驗證 App 掃描新的 QR code，輸入 App 上的 6 位數完成重新設定 (完成前舊的驗證 App 仍然有效)', 'info')
        elif action == 'confirm_2fa':
            secret = session.get(SETUP_KEY)
            if not secret or (staff.totp_enabled and not session.get(REBIND_KEY)):
                session.pop(SETUP_KEY, None)
                raise AppError('請重新開始設定')
            if not staff.totp_enabled:
                # 首次啟用要確認目前密碼 (重新綁定已在 rebind_2fa 確認過)：拿到未登出工作階段的人無法綁上自己的手機奪走帳號
                keys = _unlocked_staff_keys(staff)
                if not staff.check_password(request.form.get('current_password') or ''):
                    auth_throttle.record_staff_failure(keys)
                    raise AppError('❌ 啟用兩步驟驗證需輸入目前密碼！')
            counter = security_service.match_totp(secret, request.form.get('code'))
            if counter is None:
                raise AppError('驗證碼不正確：請確認手機時間正確，並輸入驗證 App 上目前顯示的 6 位數')
            rebind = staff.totp_enabled
            staff.totp_secret, staff.totp_enabled, staff.totp_last_counter = secret, True, counter
            new_codes, staff.totp_backup_codes = security_service.new_backup_codes()
            session.pop(SETUP_KEY, None)
            session.pop(REBIND_KEY, None)
            if rebind:
                record_staff_audit('STAFF_2FA_REBIND', details=f'{staff.username} 重新設定驗證 App')
            else:
                record_staff_audit('STAFF_2FA_ENABLE', details=f'{staff.username} 啟用兩步驟驗證')
            db.session.commit()
            flash(f"已{'重新設定驗證 App' if rebind else '啟用兩步驟驗證'}。請立即抄下備用碼並妥善保存 (只顯示這一次，舊的備用碼全部失效)",
                  'success')
        elif action == 'cancel_rebind':
            session.pop(SETUP_KEY, None)
            session.pop(REBIND_KEY, None)
        elif action == 'regen_backup':
            # 驗證碼或一組備用碼 (手機遺失時也能換一批新的備用碼)
            if not staff.totp_enabled:
                raise AppError('尚未啟用兩步驟驗證')
            _confirm_password_and_code(staff, password=False)
            new_codes, staff.totp_backup_codes = security_service.new_backup_codes()
            record_staff_audit('STAFF_2FA_BACKUP', details=f'{staff.username} 重新產生備用碼')
            db.session.commit()
            flash('已產生新的備用碼，舊的備用碼全部失效', 'success')
        elif action == 'disable_2fa':
            if not staff.totp_enabled:
                raise AppError('尚未啟用兩步驟驗證')
            if security_service.required_for(staff):
                raise AppError('本店要求您的角色必須啟用兩步驟驗證，無法停用 (手機遺失請改用「重新設定驗證 App」)')
            _confirm_password_and_code(staff)
            staff.totp_secret, staff.totp_enabled, staff.totp_backup_codes = '', False, '[]'
            record_staff_audit('STAFF_2FA_DISABLE', details=f'{staff.username} 停用兩步驟驗證')
            db.session.commit()
            flash('已停用兩步驟驗證', 'info')
            return redirect(url_for('security.account'))
        elif action == 'revoke_session':
            row = security_service.revoke_session(staff, safe_id(request.form.get('session_id'), 0))
            db.session.commit()
            flash(f'已登出 {row.device} ({row.ip_address})', 'success')
            return redirect(url_for('security.account'))
        elif action == 'logout_others':
            current = security_service.current_session_row()
            n = 0
            for row in security_service.active_sessions(staff):
                if current is None or row.id != current.id:
                    security_service.revoke_session(staff, row.id, '登出其他裝置')
                    n += 1
            db.session.commit()
            flash(f'已登出其他 {n} 個裝置', 'success')
            return redirect(url_for('security.account'))
        elif action == 'logout_all':
            security_service.logout_everywhere(staff)
            record_staff_audit('STAFF_LOGOUT_ALL', details=f'{staff.username} 登出所有裝置 (含各分店)')
            db.session.commit()
            flash('已登出所有裝置，請重新登入', 'info')
            return redirect(url_for('admin.admin_dashboard'))
        else:
            raise AppError('不支援的操作')
        if new_codes is None:
            return redirect(url_for('security.account'))
    rebinding = bool(staff.totp_enabled and session.get(REBIND_KEY))
    setup_secret = session.get(SETUP_KEY) if not staff.totp_enabled or rebinding else None
    qr = None
    if setup_secret:
        issuer = (g.tenant.name if getattr(g, 'tenant', None) else '點餐系統')[:30]
        uri = security_service.provisioning_uri(setup_secret, staff.username, issuer)
        try:
            qr = table_service.qr_data_uri(uri)
        except AppError:
            qr = None
    current = security_service.current_session_row()
    return render_template('admin_security.html', staff=staff, setup_secret=setup_secret, qr=qr, new_codes=new_codes,
                           rebinding=rebinding, owner_on_node=owner_on_node(staff),
                           backup_left=security_service.remaining_backup_codes(staff),
                           required=security_service.required_for(staff),
                           sessions=security_service.active_sessions(staff), current_id=current.id if current else None)
