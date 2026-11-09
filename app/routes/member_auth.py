"""
會員進階登入：
- LINE 登入 / 綁定 (/auth/line → LINE 授權 → /auth/line/callback)
- 簡訊驗證碼 (/api/otp/send、/api/otp/verify)，註冊驗證手機、忘記 PIN 以簡訊重設 (/forgot_pin)
"""
import hmac
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

from app.errors import AppError
from app.extensions import db, limiter
from app.models.user import User
from app.services import i18n_service, line_service, settings_service, sms_service
from app.services.audit import record_staff_audit
from app.services.auth_throttle import clear_pin_lockout
from app.services.customer_session import current_member, login_member, needs_pin_confirm
from app.sync import runtime as sync_runtime
from app.utils import WEAK_PIN_MESSAGE, get_json_dict, is_weak_pin, member_required, validate_phone, validate_pin

member_auth_bp = Blueprint('member_auth', __name__)
LINE_STATE_KEY = 'line_login'
LINE_PENDING_KEY = 'line_pending'
# LINE 授權後必須在這段時間內登入 / 註冊才會綁定；逾時或登出後作廢 (共用點餐機上不可留給下一位顧客)
LINE_PENDING_TTL = timedelta(minutes=10)
LINE_BIND_NEEDS_PIN = '您是以人臉辨識登入：綁定 LINE 前請先登出，改以手機 + PIN 登入後再綁定'
LINE_BIND_NEEDS_PIN_KIOSK = '您是在門市以 LINE 登入：綁定 LINE 前請先登出，改以手機 + PIN 登入後再綁定'


def _line_bind_needs_pin_message():
    """needs_pin_confirm() 的工作階段不可綁定 LINE：依實際登入方式 (人臉 / 門市 LINE) 提示"""
    return LINE_BIND_NEEDS_PIN_KIOSK if session.get('auth_factor') == 'line_kiosk' else LINE_BIND_NEEDS_PIN


@member_auth_bp.app_context_processor
def inject_member_features():
    def member_features():
        sms = settings_service.get('sms')
        return {
            'otp_register': sms.get('provider', 'none') != 'none' and bool(sms.get('otp_on_register')),
            'pin_reset': sms.get('provider', 'none') != 'none' and bool(sms.get('allow_pin_reset')),
            'line_login': line_service.login_enabled(),
            'line_pending': pending_line() is not None,
            'line_pending_name': (pending_line() or {}).get('name') or '',
            # 註冊 / 登入表單確認「同時綁定這個 LINE」時帶回的一次性權杖 (見 bind_pending_line)
            'line_pending_token': (pending_line() or {}).get('token') or '',
        }
    return {'member_features': member_features}


def _callback_url():
    from app.services.payment_ecpay import public_base_url
    return public_base_url() + url_for('member_auth.line_callback')


def pending_line():
    """目前有效的 LINE 待綁定資料 (同店、未逾時)；無效時清除並回傳 None"""
    pending = session.get(LINE_PENDING_KEY)
    if not isinstance(pending, dict) or not pending.get('user_id') or pending.get('tenant_id') != g.tenant_id:
        session.pop(LINE_PENDING_KEY, None)
        return None
    try:
        at = datetime.fromisoformat(str(pending.get('at')))
    except (TypeError, ValueError):
        at = None
    if at is None or datetime.now() - at > LINE_PENDING_TTL:
        session.pop(LINE_PENDING_KEY, None)
        return None
    return pending


def bind_pending_line(user, token=None):
    """
    登入 / 註冊成功後，把剛才 (10 分鐘內) LINE 授權的帳號綁定到這位會員。
    token：表單上顧客確認「同時綁定 LINE」時帶回的一次性權杖 (member_features 的 line_pending_token)；
    呼叫端有傳 token (註冊頁) 時必須與待綁定資料相符才綁定 (共用點餐機上不知情的下一位顧客不會被綁上別人的 LINE)。
    """
    pending = pending_line()
    session.pop(LINE_PENDING_KEY, None)
    if pending is None:
        return False
    if token is not None and not (pending.get('token') and hmac.compare_digest(str(token), str(pending['token']))):
        return False
    line_id = pending['user_id']
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
    if current_member() is not None and needs_pin_confirm():
        flash(_line_bind_needs_pin_message(), 'warning')
        return redirect(url_for('customer.customer_index'))
    state, nonce = line_service.new_state()
    session[LINE_STATE_KEY] = {'state': state, 'nonce': nonce, 'tenant_id': g.tenant_id}
    url = line_service.authorize_url(_callback_url(), state, nonce)
    if not sync_runtime.is_hub():
        # 門市 (共用點餐機)：不自動以瀏覽器保留的 LINE 登入狀態登入 (上一位顧客的帳號)
        url += '&disable_auto_login=true'
    return redirect(url)


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
        # 門市 (共用點餐機) 的 LINE 登入記為 line_kiosk：瀏覽器可能保留上一位顧客的 LINE 登入狀態，不視同 PIN
        login_member(user, factor='line' if sync_runtime.is_hub() else 'line_kiosk')
        flash(f'歡迎回來，{user.name}！(LINE 登入)', 'success')
        return redirect(url_for('customer.customer_index'))
    if member is not None:
        if needs_pin_confirm():
            # 人臉 (可能誤認) / 門市 LINE 登入的工作階段不可綁定 LINE：否則一次誤認就變成可長期遠端登入的強因子
            flash(_line_bind_needs_pin_message(), 'warning')
            return redirect(url_for('customer.customer_index'))
        member.line_user_id = profile['user_id']
        db.session.commit()
        flash('已綁定 LINE，之後可用 LINE 登入並接收取餐通知', 'success')
        return redirect(url_for('customer.customer_index'))
    session[LINE_PENDING_KEY] = {'user_id': profile['user_id'], 'name': profile['name'], 'tenant_id': g.tenant_id,
                                 'at': datetime.now().isoformat(), 'token': secrets.token_urlsafe(16)}
    flash('這個 LINE 帳號尚未綁定會員：請以手機 + PIN 登入，或加入會員，完成後會自動綁定 LINE。', 'info')
    return redirect(url_for('auth.member_login') if request.args.get('register') is None else url_for('auth.register'))


@member_auth_bp.route('/auth/line/pending/discard', methods=['POST'], endpoint='line_pending_discard')
def line_pending_discard():
    """顧客不是剛才授權 LINE 的人：取消待綁定 (登入後不會綁到別人的 LINE)"""
    session.pop(LINE_PENDING_KEY, None)
    flash('已取消 LINE 綁定；登入後不會綁定任何 LINE 帳號。', 'info')
    # 註冊頁的取消按鈕帶 next=register：留在註冊頁 (只接受固定值，不做開放式轉址)
    return redirect(url_for('auth.register') if request.form.get('next') == 'register' else url_for('auth.member_login'))


@member_auth_bp.route('/auth/line/unbind', methods=['POST'], endpoint='line_unbind')
@member_required('customer.customer_index')
def line_unbind(member):
    from app.services.customer_session import require_confirmed
    require_confirmed(member, request.form.get('pin'), '解除 LINE 綁定')
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
    if purpose == 'register' and not settings_service.get('sms').get('otp_on_register'):
        raise AppError('本店註冊不需簡訊驗證', 403)
    if purpose == 'register' and User.query.filter_by(phone=phone).first():
        raise AppError('此手機號碼已註冊過本店會員，請直接登入！', 409)
    if purpose == 'reset_pin' and not settings_service.get('sms').get('allow_pin_reset'):
        raise AppError('本店未開放以簡訊重設 PIN，請洽櫃檯', 403)
    sms_service.send_otp(g.tenant_id, phone, purpose, request.remote_addr or '')
    db.session.commit()
    return jsonify({'success': True, 'message': '驗證碼已寄出，10 分鐘內有效',
                    'message_i18n': i18n_service.translate_message('驗證碼已寄出，10 分鐘內有效')})


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
    flash('已重設 PIN，請用新的 PIN 登入', 'success')
    return redirect(url_for('auth.member_login'))
