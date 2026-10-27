"""
會員進階登入：
- LINE 登入 / 綁定 (/auth/line → LINE 授權 → /auth/line/callback)
- 簡訊驗證碼 (/api/otp/send、/api/otp/verify)，註冊驗證手機、忘記 PIN 以簡訊重設 (/forgot_pin)
"""
from datetime import datetime

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from app.errors import AppError
from app.extensions import db, limiter
from app.models.user import User
from app.services import line_service, settings_service, sms_service
from app.services.audit import record_staff_audit
from app.services.auth_throttle import clear_pin_lockout
from app.services.customer_session import current_member, login_member
from app.utils import WEAK_PIN_MESSAGE, get_json_dict, is_weak_pin, validate_phone, validate_pin

member_auth_bp = Blueprint('member_auth', __name__)
LINE_STATE_KEY = 'line_login'
LINE_PENDING_KEY = 'line_pending'


@member_auth_bp.app_context_processor
def inject_member_features():
    def member_features():
        sms = settings_service.get('sms')
        return {
            'otp_register': sms.get('provider', 'none') != 'none' and bool(sms.get('otp_on_register')),
            'pin_reset': sms.get('provider', 'none') != 'none' and bool(sms.get('allow_pin_reset')),
            'line_login': line_service.login_enabled(),
            'line_pending': bool(session.get(LINE_PENDING_KEY)),
        }
    return {'member_features': member_features}


def _callback_url():
    base = current_app.config.get('PUBLIC_BASE_URL') or request.url_root.rstrip('/')
    return base + url_for('member_auth.line_callback')


def bind_pending_line(user):
    """登入 / 註冊成功後，把剛才 LINE 授權的帳號綁定到這位會員"""
    pending = session.pop(LINE_PENDING_KEY, None)
    if not pending or not isinstance(pending, dict):
        return False
    line_id = pending.get('user_id')
    if not line_id or pending.get('tenant_id') != g.tenant_id:
        return False
    other = User.query.filter(User.line_user_id == line_id, User.id != user.id).first()
    if other is not None:
        other.line_user_id = ''
    user.line_user_id = line_id
    return True


@member_auth_bp.route('/auth/line', endpoint='line_start')
@limiter.limit('20/minute')
def line_start():
    if not line_service.login_enabled():
        flash('本店尚未開放 LINE 登入', 'warning')
        return redirect(url_for('customer.customer_index'))
    state, nonce = line_service.new_state()
    session[LINE_STATE_KEY] = {'state': state, 'nonce': nonce, 'tenant_id': g.tenant_id}
    return redirect(line_service.authorize_url(_callback_url(), state, nonce))


@member_auth_bp.route('/auth/line/callback', endpoint='line_callback')
@limiter.limit('20/minute')
def line_callback():
    saved = session.pop(LINE_STATE_KEY, None) or {}
    if request.args.get('error'):
        flash('已取消 LINE 登入', 'info')
        return redirect(url_for('customer.customer_index'))
    if not saved or request.args.get('state') != saved.get('state') or saved.get('tenant_id') != g.tenant_id:
        raise AppError('LINE 登入驗證失敗 (請重新點選 LINE 登入)', 400)
    profile = line_service.exchange_code(request.args.get('code', ''), _callback_url(), saved.get('nonce'))
    member = current_member()
    user = User.query.filter_by(line_user_id=profile['user_id']).first()
    if user is not None:
        login_member(user)
        flash(f'歡迎回來，{user.name}！(LINE 登入)', 'success')
        return redirect(url_for('customer.customer_index'))
    if member is not None:
        member.line_user_id = profile['user_id']
        db.session.commit()
        flash('✅ 已綁定 LINE，之後可用 LINE 登入並接收取餐通知', 'success')
        return redirect(url_for('customer.customer_index'))
    session[LINE_PENDING_KEY] = {'user_id': profile['user_id'], 'name': profile['name'], 'tenant_id': g.tenant_id,
                                 'at': datetime.now().isoformat()}
    flash('這個 LINE 帳號尚未綁定會員：請以手機 + PIN 登入，或加入會員，完成後會自動綁定 LINE。', 'info')
    return redirect(url_for('auth.member_login') if request.args.get('register') is None else url_for('auth.register'))


@member_auth_bp.route('/auth/line/unbind', methods=['POST'], endpoint='line_unbind')
def line_unbind():
    member = current_member()
    if member is None:
        return redirect(url_for('customer.customer_index'))
    member.line_user_id = ''
    db.session.commit()
    flash('已解除 LINE 綁定', 'success')
    return redirect(url_for('privacy.notice'))


@member_auth_bp.route('/api/otp/send', methods=['POST'], endpoint='otp_send')
@limiter.limit('5/minute')
def otp_send():
    data = get_json_dict()
    if not sms_service.is_enabled():
        raise AppError('本店未啟用簡訊驗證')
    purpose = data.get('purpose') if data.get('purpose') in ('register', 'reset_pin') else 'register'
    phone = str(data.get('phone') or '').strip()
    if purpose == 'register' and User.query.filter_by(phone=phone).first():
        raise AppError('此手機號碼已註冊過本店會員，請直接登入！', 409)
    if purpose == 'reset_pin' and not settings_service.get('sms').get('allow_pin_reset'):
        raise AppError('本店未開放以簡訊重設 PIN，請洽櫃檯', 403)
    sms_service.send_otp(g.tenant_id, phone, purpose, request.remote_addr or '')
    db.session.commit()
    return jsonify({'success': True, 'message': '驗證碼已寄出，10 分鐘內有效'})


@member_auth_bp.route('/forgot_pin', methods=['GET', 'POST'], endpoint='forgot_pin')
@limiter.limit('10/minute', methods=['POST'])
def forgot_pin():
    cfg = settings_service.get('sms')
    if cfg.get('provider', 'none') == 'none' or not cfg.get('allow_pin_reset'):
        flash('本店未開放以簡訊重設 PIN，請洽櫃檯人員協助', 'info')
        return redirect(url_for('auth.member_login'))
    if request.method == 'GET':
        return render_template('forgot_pin.html')
    phone = (request.form.get('phone') or '').strip()
    pin = (request.form.get('pin') or '').strip()
    if not validate_phone(phone):
        raise AppError('手機號碼格式不正確')
    if not validate_pin(pin):
        raise AppError('新的 PIN 碼必須為 4 至 6 位數字')
    if is_weak_pin(pin, phone):
        raise AppError(WEAK_PIN_MESSAGE)
    user = User.query.filter_by(phone=phone).first()
    sms_service.consume_verified(phone, 'reset_pin', request.form.get('otp_code'))
    if user is None:
        db.session.commit()
        raise AppError('查無此手機號碼的會員', 404)
    user.set_pin(pin)
    user.phone_verified_at = datetime.now()
    record_staff_audit('MEMBER_RESET_PIN', target_user_id=user.id, as_system=True, tenant_id=g.tenant_id,
                       details=f'會員 {user.name} 以簡訊驗證重設 PIN')
    db.session.commit()
    clear_pin_lockout(g.tenant_id, phone)
    flash('✅ 已重設 PIN，請用新的 PIN 登入', 'success')
    return redirect(url_for('auth.member_login'))
