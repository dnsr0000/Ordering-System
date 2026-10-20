import os
import uuid
import time
import threading
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, g
from app.config import Config
from app.extensions import db
from app.models.user import User
from app.services.cv_service import save_and_fix_image, extract_feature, find_best_match_vectorized
from app.routes.common import (
    start_customer_session, clear_customer_session, 
    close_customer_session, refund_orphaned_redemptions
)
from app.utils import rate_limit, js_alert_redirect, validate_pin, validate_phone, get_json_dict

auth_bp = Blueprint('auth', __name__)

# 註冊時的「重複人臉」檢查門檻。比登入的 0.363 嚴格：登入是「確認是誰」，這裡是「擋下已註冊過的同一個人」，
# 誤擋 (長得像的兩個人) 比漏擋更傷客人，所以只擋相似度很高的情況。
DUPLICATE_FACE_THRESHOLD = 0.45

_PIN_LOCK_MUTEX = threading.Lock()
# 失敗狀態表，三種鍵 (第 0、1 欄固定為 店家 ID、手機，方便 clear_pin_lockout 一次清除該會員所有鎖定)：
#   (tid, phone, ip)    同一裝置/IP 對同一手機 —— 連續錯誤達 _PIN_DEVICE_MAX_FAILS 次鎖定
#   (tid, phone)        同一手機「不分來源 IP」累計錯誤 —— 達 _PIN_ACCOUNT_MAX_FAILS 次鎖定，且每次再鎖時間加倍
#   (tid, '*ip*', ip)   同一 IP 對「任意手機」累計錯誤 —— 防止一個 IP 噴灑式猜測
_PIN_FAIL_STATE = {}

_PIN_DEVICE_MAX_FAILS = 5
_PIN_ACCOUNT_MAX_FAILS = 10
_PIN_IP_MAX_FAILS = 30          # 門市 kiosk 共用同一 IP，門檻放寬並以時間窗衰減，避免誤鎖全店
_PIN_FAIL_WINDOW = 15 * 60      # 失敗次數的統計時間窗 (秒)
_PIN_LOCK_SECONDS = 15 * 60     # 基本鎖定時間
_PIN_LOCK_MAX_SECONDS = 24 * 60 * 60
_PIN_STATE_TTL = 24 * 60 * 60   # 閒置超過此時間的紀錄會被清除，避免記憶體無限增長


def _pin_lock_remaining(key, now_ts):
    """(需持有 _PIN_LOCK_MUTEX) 回傳此鍵剩餘鎖定秒數，未鎖定回傳 0"""
    st = _PIN_FAIL_STATE.get(key)
    if st and now_ts < st.get('locked_until', 0):
        return st['locked_until'] - now_ts
    return 0


def _pin_record_failure(key, max_fails, now_ts, escalate=False):
    """(需持有 _PIN_LOCK_MUTEX) 記錄一次失敗；達上限即鎖定。escalate=True 時每次再鎖定的時間加倍 (上限 24 小時)"""
    st = _PIN_FAIL_STATE.get(key)
    if st is None:
        st = {'fails': 0, 'strikes': 0, 'locked_until': 0, 'window_start': now_ts, 'last_ts': now_ts}
        _PIN_FAIL_STATE[key] = st
    if now_ts >= st['locked_until'] and now_ts - st['window_start'] > _PIN_FAIL_WINDOW:
        st['fails'] = 0
        st['window_start'] = now_ts
    st['last_ts'] = now_ts
    st['fails'] += 1
    if st['fails'] >= max_fails:
        st['strikes'] += 1
        duration = _PIN_LOCK_SECONDS * (2 ** (st['strikes'] - 1)) if escalate else _PIN_LOCK_SECONDS
        st['locked_until'] = now_ts + min(duration, _PIN_LOCK_MAX_SECONDS)
        st['fails'] = 0
        st['window_start'] = now_ts


def _pin_purge_stale(now_ts):
    """(需持有 _PIN_LOCK_MUTEX) 狀態表過大時，清掉已解鎖且閒置超過 TTL 的紀錄"""
    if len(_PIN_FAIL_STATE) < 2000:
        return
    stale = [
        k for k, st in _PIN_FAIL_STATE.items()
        if now_ts >= st.get('locked_until', 0) and now_ts - st.get('last_ts', 0) > _PIN_STATE_TTL
    ]
    for k in stale:
        _PIN_FAIL_STATE.pop(k, None)


def get_face_embedding(filepath):
    return extract_feature(filepath)


def clear_pin_lockout(tenant_id, phone):
    """櫃檯協助設定/重設 PIN 時，一併清除該會員的裝置鎖定與帳號鎖定 (不影響 IP 層級的噴灑防護)"""
    phone_key = str(phone).strip()
    with _PIN_LOCK_MUTEX:
        keys_to_remove = [k for k in _PIN_FAIL_STATE if k[0] == tenant_id and k[1] == phone_key]
        for k in keys_to_remove:
            _PIN_FAIL_STATE.pop(k, None)
@auth_bp.route('/register', methods=['GET', 'POST'], endpoint='register')
@rate_limit(max_requests=10, window_seconds=60, key_prefix="auth_register", methods=('POST',))
def register():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        pin = (request.form.get('pin') or '').strip()
        photo = request.files.get('photo')

        if not name or not phone or not pin or not photo or photo.filename == '':
            return js_alert_redirect('請填寫完整資訊並拍攝/上傳照片！', status=400)

        if not validate_phone(phone):
            return js_alert_redirect('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。', status=400)

        if not validate_pin(pin):
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

        # 同一張臉不可用不同手機重複註冊 (否則每個號碼都能領一次 20 點註冊禮，且同臉多帳號會讓刷臉登入進錯帳號)
        existing_members = User.query.filter(User.feature.isnot(None)).all()
        dup_member, _dup_score = find_best_match_vectorized(feature, existing_members, threshold=DUPLICATE_FACE_THRESHOLD)
        if dup_member is not None:
            try:
                os.remove(filepath)
            except OSError:
                pass
            return js_alert_redirect('⚠️ 此人臉已註冊為本店會員，請直接使用「人臉辨識」或「手機 + PIN」登入；如需協助請洽櫃檯。', status=409)

        new_user = User(
            name=name[:100],
            phone=phone,
            photo_path=filename,
            feature=feature,
            points=20,
            last_login_at=datetime.now()
        )
        new_user.set_pin(pin)
        db.session.add(new_user)
        try:
            db.session.commit()
        except IntegrityError:
            # 「查電話是否存在」與「寫入」之間有空窗：兩個人同時註冊同一支手機時，後到的會在這裡撞上唯一索引
            db.session.rollback()
            try:
                os.remove(filepath)
            except OSError:
                pass
            return js_alert_redirect('❌ 此手機號碼已註冊過本店會員，請直接登入！', status=409)
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
    data = get_json_dict()
    # 長度截斷：phone/pin 會成為鎖定狀態表的鍵，避免被塞入超長字串耗用記憶體
    phone = str(data.get('phone', '')).strip()[:32]
    pin = str(data.get('pin', '')).strip()[:32]
    if not phone or not pin:
        return jsonify({'success': False, 'message': '請輸入手機號碼與安全 PIN 碼！'}), 400

    tid = getattr(g, 'tenant_id', None) or session.get('tenant_id') or 1
    client_ip = request.remote_addr or '127.0.0.1'
    device_key = (tid, phone, client_ip)
    account_key = (tid, phone)
    ip_key = (tid, '*ip*', client_ip)
    now_ts = time.time()

    # 任一層級處於鎖定中即拒絕 (連「正確 PIN」也不放行)：
    # 帳號層級不依賴來源 IP，因此即使攻擊者不斷更換 IP 也無法繞過
    with _PIN_LOCK_MUTEX:
        _pin_purge_stale(now_ts)
        remaining = max(_pin_lock_remaining(k, now_ts) for k in (device_key, account_key, ip_key))
        if remaining > 0:
            rem_min = max(1, int((remaining + 59) // 60))
            return jsonify({
                'success': False,
                'message': f'⚠️ PIN 碼錯誤次數過多，此帳號或裝置已暫時鎖定，請於 {rem_min} 分鐘後再試，或改用人臉辨識登入！'
            }), 429

    user = User.query.filter_by(phone=phone).first()

    # 若為尚未設定 PIN 碼的歷史舊會員，回傳 403 提示改用人臉辨識或洽櫃檯人員設定 PIN
    # (此分支仍計入 IP 層級失敗次數，避免被拿來無限列舉有哪些手機號碼是會員)
    if user and not user.pin_hash:
        with _PIN_LOCK_MUTEX:
            _pin_record_failure(ip_key, _PIN_IP_MAX_FAILS, now_ts)
        return jsonify({
            'success': False,
            'message': '此舊版會員帳號尚未設定安全 PIN 碼，請改用人臉辨識登入，或洽詢櫃檯人員協助設定！'
        }), 403

    if not user or not user.check_pin(pin):
        # 不論手機號碼是否存在都同樣計數，使「空號」與「已註冊」的鎖定行為一致，無法藉此列舉會員
        with _PIN_LOCK_MUTEX:
            _pin_record_failure(device_key, _PIN_DEVICE_MAX_FAILS, now_ts)
            _pin_record_failure(account_key, _PIN_ACCOUNT_MAX_FAILS, now_ts, escalate=True)
            _pin_record_failure(ip_key, _PIN_IP_MAX_FAILS, now_ts)
        return jsonify({'success': False, 'message': '手機號碼或安全 PIN 碼不正確！'}), 401

    # 登入成功：清除該會員的裝置/帳號失敗紀錄 (IP 層級不重置，避免攻擊者穿插自己的正確登入來洗掉計數)
    with _PIN_LOCK_MUTEX:
        _PIN_FAIL_STATE.pop(device_key, None)
        _PIN_FAIL_STATE.pop(account_key, None)
    
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