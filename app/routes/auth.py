import os
import re
import uuid
import time
import threading
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, g
from app.config import Config
from app.extensions import db
from app.models.user import User
from app.services.cv_service import save_and_fix_image, extract_feature, find_best_match_vectorized
from app.routes.common import (
    start_customer_session, clear_customer_session, 
    close_customer_session, refund_orphaned_redemptions
)
from app.utils import rate_limit, js_alert_redirect

auth_bp = Blueprint('auth', __name__)

PHONE_REGEX = re.compile(r'^09\d{8}$')
PIN_REGEX = re.compile(r'^\d{4,6}$')

_PIN_LOCK_MUTEX = threading.Lock()
_PIN_FAIL_STATE = {}  # (tenant_id, phone, ip) -> {'fails': int, 'locked_until': float}


def get_face_embedding(filepath):
    return extract_feature(filepath)


def clear_pin_lockout(tenant_id, phone):
    with _PIN_LOCK_MUTEX:
        keys_to_remove = [k for k in _PIN_FAIL_STATE if k[0] == tenant_id and k[1] == str(phone).strip()]
        for k in keys_to_remove:
            _PIN_FAIL_STATE.pop(k, None)


@auth_bp.route('/register', methods=['GET', 'POST'], endpoint='register')
@rate_limit(max_requests=10, window_seconds=60, key_prefix="auth_register", methods=('POST',))
def register():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        pin = (request.form.get('pin') or '0000').strip()
        photo = request.files.get('photo')

        if not name or not phone or not photo or photo.filename == '':
            return js_alert_redirect('請填寫完整資訊並拍攝/上傳照片！', status=400)

        if not PHONE_REGEX.match(phone):
            return js_alert_redirect('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。', status=400)

        if not PIN_REGEX.match(pin):
            return js_alert_redirect('❌ 安全 PIN 碼必須為 4 至 6 位純數字！', status=400)

        if User.query.filter_by(phone=phone).first():
            return js_alert_redirect('❌ 此手機號碼已註冊過本店會員，請直接登入！', status=409)

        # 使用隨機 UUID 作為檔名，徹底阻斷路徑穿越 (Path Traversal) 與可預測檔名風險
        filename = f"member_{uuid.uuid4().hex[:16]}.jpg"
        filepath = os.path.join(Config.UPLOAD_FOLDER_MEMBER, filename)

        if not save_and_fix_image(photo, filepath):
            return js_alert_redirect('❌ 上傳的相片檔案已損壞或格式不支援，請上傳清晰的 JPG/PNG 圖片！', status=400)

        feature = extract_feature(filepath)
        if feature is None:
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            return js_alert_redirect('⚠️ 照片未偵測到清晰人臉！請正對鏡頭並重新拍攝。', status=400)

        new_user = User(
            name=name,
            phone=phone,
            photo_path=filename,
            feature=feature,
            points=20,
            last_login_at=datetime.now()
        )
        new_user.set_pin(pin)
        db.session.add(new_user)
        db.session.commit()
        start_customer_session('會員', f"{new_user.name} ({new_user.phone})")

        session['user_id'] = new_user.id
        session['user_name'] = new_user.name
        session['user_points'] = new_user.points
        session['is_guest'] = False
        return redirect(url_for('customer.customer_index'))

    return render_template('register.html', login_mode=False)


@auth_bp.route('/face_login', methods=['GET', 'POST'], endpoint='face_login')
@rate_limit(max_requests=15, window_seconds=60, key_prefix="auth_face_login", methods=('POST',))
def face_login():
    if request.method == 'POST':
        photo = request.files.get('photo')
        if not photo:
            return jsonify({'success': False, 'message': '未接收到拍攝照片'}), 400

        temp_path = os.path.join(Config.UPLOAD_FOLDER_MEMBER, f"temp_{uuid.uuid4().hex[:12]}.jpg")
        save_and_fix_image(photo, temp_path)
        curr_feature = extract_feature(temp_path)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        if curr_feature is None:
            return jsonify({'success': False, 'message': '未在畫面中偵測到清晰人臉，請正對鏡頭！'}), 400

        users = User.query.filter(User.feature.isnot(None)).all()
        matched_user, best_score = find_best_match_vectorized(curr_feature, users, threshold=0.363)

        if matched_user:
            refund_orphaned_redemptions(matched_user)
            matched_user.last_login_at = datetime.now()
            matched_user.last_logout_at = None
            db.session.commit()

            start_customer_session('會員', f"{matched_user.name} ({matched_user.phone})")

            session['user_id'] = matched_user.id
            session['user_name'] = matched_user.name
            session['user_points'] = matched_user.points
            session['is_guest'] = False
            return jsonify({'success': True, 'user_name': matched_user.name, 'points': matched_user.points})

        return jsonify({'success': False, 'message': '人臉比對未通過，請先註冊或重新對準鏡頭！'}), 401

    return render_template('register.html', login_mode=True)


@auth_bp.route('/phone_login', methods=['POST'], endpoint='phone_login')
@rate_limit(max_requests=15, window_seconds=60, key_prefix="auth_phone_login", methods=('POST',))
def phone_login():
    data = request.get_json() or {}
    phone = str(data.get('phone', '')).strip()
    pin = str(data.get('pin', '')).strip()
    if not phone or not pin:
        return jsonify({'success': False, 'message': '請輸入手機號碼與安全 PIN 碼！'}), 400

    tid = getattr(g, 'tenant_id', None) or session.get('tenant_id') or 1
    client_ip = request.remote_addr or '127.0.0.1'
    lock_key = (tid, phone, client_ip)
    now_ts = time.time()

    with _PIN_LOCK_MUTEX:
        st = _PIN_FAIL_STATE.get(lock_key)
        if st and now_ts < st.get('locked_until', 0):
            rem_min = max(1, int((st['locked_until'] - now_ts + 59) // 60))
            return jsonify({
                'success': False,
                'message': f'⚠️ PIN 碼錯誤次數過多，此裝置已暫時鎖定，請於 {rem_min} 分鐘後再試，或改用人臉辨識登入！'
            }), 429

    user = User.query.filter_by(phone=phone).first()

    # 若為尚未設定 PIN 碼的歷史舊會員，回傳 403 提示改用人臉辨識或洽櫃檯人員設定 PIN
    if user and not user.pin_hash:
        return jsonify({
            'success': False,
            'message': '此舊版會員帳號尚未設定安全 PIN 碼，請改用人臉辨識登入，或洽詢櫃檯人員協助設定！'
        }), 403

    if not user or not user.check_pin(pin):
        with _PIN_LOCK_MUTEX:
            st = _PIN_FAIL_STATE.setdefault(lock_key, {'fails': 0, 'locked_until': 0})
            st['fails'] += 1
            if st['fails'] >= 5:
                st['locked_until'] = now_ts + 15 * 60
        return jsonify({'success': False, 'message': '手機號碼或安全 PIN 碼不正確！'}), 401

    with _PIN_LOCK_MUTEX:
        _PIN_FAIL_STATE.pop(lock_key, None)
    
    refund_orphaned_redemptions(user)
    user.last_login_at = datetime.now()
    user.last_logout_at = None
    db.session.commit()

    start_customer_session('會員', f"{user.name} ({user.phone})")

    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_points'] = user.points
    session['is_guest'] = False
    return jsonify({'success': True, 'user_name': user.name, 'points': user.points})


@auth_bp.route('/guest_login', endpoint='guest_login')
def guest_login():
    import random
    random_guest_id = f"訪客-{random.randint(1000, 9999)}"
    start_customer_session('訪客', random_guest_id)
    session['user_id'] = None
    session['user_name'] = random_guest_id
    session['is_guest'] = True
    session['user_points'] = 0
    return redirect(url_for('customer.customer_index'))


@auth_bp.route('/logout', endpoint='logout')
def logout():
    close_customer_session()
    clear_customer_session()
    return redirect(url_for('customer.customer_index'))
