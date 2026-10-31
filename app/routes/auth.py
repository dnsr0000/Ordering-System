"""Kiosk 會員：註冊、人臉登入、手機 + PIN 登入、訪客、登出 (線上端只提供手機 + PIN 登入與訪客)"""
import os
import uuid

from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db, limiter
from app.models.user import User
from app.services import auth_throttle, privacy_service
from app.services.customer_session import close_customer_session, login_guest, login_member
from app.services.cv_service import extract_feature, find_best_match_vectorized, save_and_fix_image
from app.services.menu_service import remove_file_quietly
from app.sync import runtime as sync_runtime
from app.utils import WEAK_PIN_MESSAGE, get_json_dict, is_weak_pin, validate_phone, validate_pin

auth_bp = Blueprint('auth', __name__)

FACE_MATCH_THRESHOLD = 0.363
# 註冊時的「重複人臉」門檻比登入嚴格：誤擋長得像的兩個人比漏擋更傷客人，只擋相似度很高的情況
DUPLICATE_FACE_THRESHOLD = 0.45


def face_required(fn):
    """線上端 (hub) 沒有門市相機：不提供人臉註冊與人臉登入 (POST 一律 404，開啟頁面則導回並說明)"""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if sync_runtime.is_hub():
            if request.method != 'GET':
                abort(404)
            flash('線上點餐不提供人臉登入：請以手機號碼 + PIN 登入，尚未加入會員可直接線上註冊。', 'info')
            return redirect(url_for('auth.member_login'))
        return fn(*args, **kwargs)
    return wrapped


def member_login_url():
    """會員登入頁：線上端為手機 + PIN 登入頁，其他角色為人臉 / 手機登入頁"""
    return url_for('auth.member_login') if sync_runtime.is_hub() else url_for('auth.face_login')


def _member_photo_path(filename):
    return os.path.join(current_app.config['UPLOAD_FOLDER_MEMBER'], filename)


def _face_feature_from_upload(photo, filepath):
    """儲存上傳照片並擷取人臉特徵；照片損毀拋出 AppError，沒有人臉回傳 None"""
    if not save_and_fix_image(photo, filepath):
        raise AppError('❌ 上傳的相片檔案已損壞或格式不支援，請上傳清晰的 JPG/PNG 圖片！')
    return extract_feature(filepath)


@auth_bp.route('/register', methods=['GET', 'POST'], endpoint='register')
@limiter.limit('10/minute', methods=['POST'])
def register():
    """
    加入會員：姓名、手機、PIN 為必填，並須同意隱私權告知；人臉辨識另行勾選同意才拍照 (可不提供)。
    線上端沒有門市相機，只提供手機 + PIN 註冊。
    """
    face_enabled = not sync_runtime.is_hub()
    from app.services import loyalty_service
    referral_enabled = bool(loyalty_service.referral_config(g.tenant_id).get('referral_enabled'))
    if request.method == 'GET':
        return render_template('register.html', login_mode=False, face_enabled=face_enabled,
                               privacy=privacy_service.notice_context(g.tenant_id), referral_enabled=referral_enabled,
                               referral_code=(request.args.get('ref') or '')[:12])

    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip()
    pin = (request.form.get('pin') or '').strip()
    use_face = face_enabled and bool(request.form.get('face_consent'))
    photo = request.files.get('photo') if use_face else None
    if not request.form.get('agree_privacy'):
        raise AppError('請先閱讀並勾選同意「隱私權告知」！')
    if not name or not phone or not pin:
        raise AppError('請填寫姓名、手機號碼與 PIN 碼！')
    if use_face and (not photo or not photo.filename):
        raise AppError('您勾選了人臉辨識登入，請拍攝或上傳照片 (或取消勾選，改用手機 + PIN 登入)！')
    if not validate_phone(phone):
        raise AppError('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。')
    if not validate_pin(pin):
        raise AppError('❌ 安全 PIN 碼必須為 4 至 6 位純數字！')
    if is_weak_pin(pin, phone):
        raise AppError(WEAK_PIN_MESSAGE)
    referrer = loyalty_service.find_referrer(g.tenant_id, request.form.get('referral_code')) if referral_enabled else None
    duplicate_phone = AppError('❌ 此手機號碼已註冊過本店會員，請直接登入！', 409)
    if User.query.filter_by(phone=phone).first():
        raise duplicate_phone
    from app.services import settings_service, sms_service
    sms_cfg = settings_service.get('sms')
    phone_verified = False
    if sms_cfg.get('provider', 'none') != 'none' and sms_cfg.get('otp_on_register'):
        sms_service.consume_verified(phone, 'register', request.form.get('otp_code'))
        phone_verified = True
    birthday = None
    if request.form.get('birthday'):
        from datetime import date
        try:
            birthday = date.fromisoformat(request.form.get('birthday'))
        except ValueError:
            raise AppError('生日格式不正確')

    filename, filepath, feature = '', None, None
    if use_face:
        # 隨機檔名：杜絕路徑穿越與可預測檔名
        filename = f"member_{uuid.uuid4().hex[:16]}.jpg"
        filepath = _member_photo_path(filename)
        feature = _face_feature_from_upload(photo, filepath)
        if feature is None:
            remove_file_quietly(filepath)
            raise AppError('⚠️ 照片未偵測到清晰人臉！請正對鏡頭並重新拍攝。')

        # 同一張臉不可用不同手機重複註冊 (否則每支號碼都能領註冊禮，也會讓刷臉登入進錯帳號)
        dup, _score = find_best_match_vectorized(feature, User.query.filter(User.feature.isnot(None)).all(),
                                                 threshold=DUPLICATE_FACE_THRESHOLD)
        if dup is not None:
            remove_file_quietly(filepath)
            raise AppError('⚠️ 此人臉已註冊為本店會員，請直接使用「人臉辨識」或「手機 + PIN」登入；如需協助請洽櫃檯。', 409)

    user = User(name=name[:100], phone=phone, photo_path=filename, feature=feature, points=20, birthday=birthday)
    if phone_verified:
        from datetime import datetime
        user.phone_verified_at = datetime.now()
    if referrer is not None:
        user.referred_by_uuid = referrer.uuid
    user.set_pin(pin)
    from app.routes.member_auth import bind_pending_line
    bind_pending_line(user)
    privacy_service.record_consent(user, face=use_face)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:  # 兩個人同時註冊同一支手機：後到的撞上唯一索引
        db.session.rollback()
        if filepath:
            remove_file_quietly(filepath)
        raise duplicate_phone
    login_member(user)
    return redirect(url_for('customer.customer_index'))


@auth_bp.route('/face_login', methods=['GET', 'POST'], endpoint='face_login')
@face_required
@limiter.limit('15/minute', methods=['POST'])
def face_login():
    if request.method == 'GET':
        return render_template('register.html', login_mode=True, face_enabled=True)
    photo = request.files.get('photo')
    if not photo:
        raise AppError('未接收到拍攝照片')
    temp_path = _member_photo_path(f"temp_{uuid.uuid4().hex[:12]}.jpg")
    try:
        feature = _face_feature_from_upload(photo, temp_path)
    finally:
        remove_file_quietly(temp_path)
    if feature is None:
        raise AppError('未在畫面中偵測到清晰人臉，請正對鏡頭！')

    user, _score = find_best_match_vectorized(feature, User.query.filter(User.feature.isnot(None)).all(),
                                              threshold=FACE_MATCH_THRESHOLD)
    if user is None:
        raise AppError('人臉比對未通過，請先註冊或重新對準鏡頭！', 401)
    login_member(user, factor='face')
    return jsonify({'success': True, 'user_name': user.name, 'points': user.points})


@auth_bp.route('/member_login', endpoint='member_login')
def member_login():
    """線上端的會員登入頁 (只有手機 + PIN)；其他角色沿用人臉 / 手機登入頁"""
    if not sync_runtime.is_hub():
        return redirect(url_for('auth.face_login'))
    return render_template('register.html', login_mode=True, face_enabled=False)


@auth_bp.route('/phone_login', methods=['POST'], endpoint='phone_login')
@limiter.limit('15/minute')
def phone_login():
    data = get_json_dict()
    phone = str(data.get('phone', '')).strip()[:32]
    pin = str(data.get('pin', '')).strip()[:32]
    if not phone or not pin:
        raise AppError('請輸入手機號碼與安全 PIN 碼！')

    keys = auth_throttle.pin_keys(g.tenant_id, phone, request.remote_addr or '-')
    remaining = auth_throttle.lock_remaining(keys.values())
    if remaining:  # 鎖定期間連正確 PIN 也不放行
        minutes = max(1, int((remaining.total_seconds() + 59) // 60))
        raise AppError(f'⚠️ PIN 碼錯誤次數過多，此帳號或裝置已暫時鎖定，請於 {minutes} 分鐘後再試，或改用人臉辨識登入！', 429)

    user = User.query.filter_by(phone=phone).first()
    if user is not None and not user.pin_hash:
        # 舊版會員尚未設定 PIN：提示改用人臉或洽櫃檯 (仍計入 IP 層級失敗次數，避免被拿來列舉會員)
        auth_throttle.record_pin_failure(keys, ip_only=True)
        raise AppError('此舊版會員帳號尚未設定安全 PIN 碼，請改用人臉辨識登入，或洽詢櫃檯人員協助設定！', 403)
    if user is None:
        auth_throttle.spend_hash_time(pin)  # 回應時間與手機已註冊時相同，無法藉此列舉會員
    if user is None or not user.check_pin(pin):
        # 不論手機是否存在都同樣計數，「空號」與「已註冊」的鎖定行為一致
        auth_throttle.record_pin_failure(keys)
        raise AppError('手機號碼或安全 PIN 碼不正確！', 401)

    # 登入成功只清除裝置與帳號層級 (IP 層級不清，避免攻擊者穿插自己的正確登入洗掉計數)
    auth_throttle.clear_keys(keys['device'], keys['account'])
    from app.routes.member_auth import bind_pending_line
    if bind_pending_line(user):
        db.session.commit()
    login_member(user)
    return jsonify({'success': True, 'user_name': user.name, 'points': user.points})


@auth_bp.route('/guest_login', methods=['POST'], endpoint='guest_login')
def guest_login():
    if sync_runtime.is_hub():  # 線上點餐只開放會員；訪客請至門市 (含以手機連門市網站) 點餐
        flash('線上點餐僅限會員，請登入或加入會員。', 'info')
        return redirect(url_for('auth.member_login'))
    login_guest()
    return redirect(url_for('customer.customer_index'))


@auth_bp.route('/logout', methods=['POST'], endpoint='logout')
def logout():
    close_customer_session()
    return redirect(url_for('customer.customer_index'))
