import os
import time
import json
import re
import threading
from functools import wraps
from flask import session, redirect, url_for, request, jsonify, g, current_app

ALLOWED_PAYMENT_METHODS = {
    'Cash', 'Credit Card', 'Line Pay', '街口支付',
    'CreditCard', 'LinePay', 'Apple Pay', 'JKOPay'
}
ALLOWED_ORDER_TYPES = {'內用', '外帶'}

_LOCAL_RATE_LOCK = threading.Lock()
_LOCAL_RATE_BUCKETS = {}

# 會員驗證常數
PHONE_REGEX = re.compile(r'^09\d{8}$')
PIN_REGEX = re.compile(r'^\d{4,6}$')

def safe_int(val, default=0):
    try:
        if val is None or val == '':
            return default
        return int(float(val))
    except (ValueError, TypeError, OverflowError):  # float('inf') / '1e999' 會丟 OverflowError
        return default


def safe_float(val, default=0.0):
    """安全轉浮點數：空值、格式錯誤、NaN、Infinity 一律回傳 default"""
    try:
        if val is None or val == '':
            return default
        num = float(val)
    except (ValueError, TypeError, OverflowError):
        return default
    if num != num or num in (float('inf'), float('-inf')):
        return default
    return num


def safe_round(val, default=0):
    """四捨五入成整數；空值/格式錯誤/NaN/Infinity 回傳 default"""
    num = safe_float(val, None)
    return default if num is None else int(round(num))


def get_json_dict():
    """讀取請求的 JSON 本文：內容不是合法 JSON、Content-Type 不符或根節點不是物件時，一律回傳 {} (避免 400/415 HTML 錯誤頁與 AttributeError)"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _js_string_literal(value):
    """產生可安全嵌入 <script> 的 JS 字串常值：額外跳脫 < > & 與 U+2028/2029，避免 </script> 提前結束腳本區塊"""
    return (
        json.dumps(str(value), ensure_ascii=False)
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('&', '\\u0026')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


def js_alert_redirect(msg, target_url=None, status=200):
    safe_msg = _js_string_literal(msg)
    if target_url:
        safe_url = _js_string_literal(target_url)
        return f"<script>alert({safe_msg}); window.location.href={safe_url};</script>", status
    return f"<script>alert({safe_msg}); window.history.back();</script>", status


def get_client_ip():
    return request.remote_addr or '127.0.0.1'


def _check_rate_limit_redis_or_local(bucket_key, max_requests, window_seconds):
    if os.getenv("DISABLE_RATE_LIMIT") == "1":
        return True, 0
    try:
        if current_app and current_app.config.get("DISABLE_RATE_LIMIT"):
            return True, 0
    except Exception:
        pass

    from app.extensions import redis_client
    now = time.time()
    if redis_client:
        try:
            pipe = redis_client.pipeline()
            pipe.incr(bucket_key)
            pipe.ttl(bucket_key)
            count, ttl = pipe.execute()
            if count == 1 or ttl < 0:
                redis_client.expire(bucket_key, window_seconds)
                ttl = window_seconds
            if count > max_requests:
                return False, max(1, int(ttl))
            return True, 0
        except Exception:
            pass

    with _LOCAL_RATE_LOCK:
        if len(_LOCAL_RATE_BUCKETS) > 5000:  # 過期的計數桶定期清除，避免來源 IP 很多時記憶體無限增長
            for stale_key in [k for k, v in _LOCAL_RATE_BUCKETS.items() if now >= v['reset_at']]:
                _LOCAL_RATE_BUCKETS.pop(stale_key, None)
        entry = _LOCAL_RATE_BUCKETS.get(bucket_key)
        if not entry or now >= entry['reset_at']:
            _LOCAL_RATE_BUCKETS[bucket_key] = {'count': 1, 'reset_at': now + window_seconds}
            return True, 0
        entry['count'] += 1
        if entry['count'] > max_requests:
            retry_after = max(1, int(entry['reset_at'] - now))
            return False, retry_after
        return True, 0


def rate_limit(max_requests=30, window_seconds=60, key_prefix="rl", methods=('POST', 'GET')):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if request.method not in methods:
                return f(*args, **kwargs)
            ip = get_client_ip()
            tid = getattr(g, 'tenant_id', None) or session.get('tenant_id') or '0'
            namespace = current_app.config.get('RATE_LIMIT_NAMESPACE', '')
            namespace_prefix = f"{namespace}:" if namespace else ''
            b_key = f"ratelimit:{namespace_prefix}{key_prefix}:{tid}:{ip}"
            allowed, retry_after = _check_rate_limit_redis_or_local(b_key, max_requests, window_seconds)
            if not allowed:
                msg = f"請求過於頻繁，請於 {retry_after} 秒後再試！"
                if request.is_json or request.path.startswith('/api/') or request.path in ('/phone_login', '/submit_order'):
                    resp = jsonify({'success': False, 'error': msg, 'message': msg, 'retry_after': retry_after})
                    resp.status_code = 429
                    resp.headers['Retry-After'] = str(retry_after)
                    return resp
                return js_alert_redirect(f"⚠️ {msg}", status=429)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in') or not session.get('staff_account_id') or not session.get('tenant_id'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': '未授權操作，請先登入管理員！'}), 401
            return redirect(url_for('admin.admin_dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def role_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not session.get('admin_logged_in') or not session.get('staff_account_id') or not session.get('tenant_id'):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'message': '未授權操作，請先登入員工帳號！'}), 401
                return redirect(url_for('admin.admin_dashboard'))

            staff = getattr(g, 'staff_account', None)
            role = getattr(staff, 'role', None) or session.get('admin_role') or session.get('staff_role') or ''
            if allowed_roles and role not in allowed_roles:
                msg = f"權限不足：您的角色 ({role}) 無法執行此操作！"
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': msg, 'message': msg}), 403
                return js_alert_redirect(f"❌ {msg}", url_for('admin.admin_dashboard'), status=403)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def is_platform_admin(staff=None, tenant=None):
    """
    平台管理者：預設店家 (slug=default，系統部署者自己的店) 的負責人。
    整庫備份/還原會觸及所有店家的資料，因此只有平台管理者可以使用；
    其他店家的負責人請改用 Excel 匯出 (僅含自己店家的資料)。
    """
    staff = staff if staff is not None else getattr(g, 'staff_account', None)
    tenant = tenant if tenant is not None else getattr(g, 'tenant', None)
    return bool(
        staff and tenant
        and getattr(staff, 'role', None) == 'owner'
        and getattr(tenant, 'slug', None) == 'default'
    )


def platform_admin_required(f):
    """需搭配 @role_required('owner') 使用：僅平台管理者可執行 (整庫備份/還原等跨店家操作)"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not is_platform_admin():
            msg = '權限不足：整庫備份與還原會涉及所有店家的資料，僅限平台管理者 (預設店家負責人) 使用；請改用 Excel 匯出您店家的資料。'
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': msg, 'message': msg}), 403
            return js_alert_redirect(f"❌ {msg}", url_for('admin.admin_dashboard'), status=403)
        return f(*args, **kwargs)
    return wrapped


def mask_name(name):
    """姓名遮罩 (王小明 → 王○明、王小 → 王○)，供公開叫號看板這類任何人都看得到的畫面使用"""
    chars = list((name or '').strip())
    if len(chars) <= 1:
        return ''.join(chars)
    if len(chars) == 2:
        return chars[0] + '○'
    return chars[0] + '○' * (len(chars) - 2) + chars[-1]


def public_display_name(order):
    """公開看板顯示用名稱：會員一律遮罩姓名；訪客編號、櫃檯 POS、桌號 (純英數) 原樣顯示"""
    if order.user_id:
        user = order.user
        return mask_name(user.name if user else (order.table_number or ''))
    table = (order.table_number or '').strip() or '訪客'
    if table.startswith(('訪客', '櫃檯POS', 'POS')) and '(' not in table and '（' not in table:
        return table
    if re.fullmatch(r'[A-Za-z0-9_\-]{1,10}', table):
        return table
    # 其他情況 (例如 "A5 (王小明)"、"王小明 (已註銷會員)")：保留括號外的桌號/代號，姓名部分遮罩
    base = re.split(r'\s*[(（]', table)[0]
    return mask_name(base)


def validate_pin(pin):
    """驗證 PIN 碼是否為 4 至 6 位純數字"""
    if pin is None:
        return False
    return bool(PIN_REGEX.match(str(pin).strip()))


def validate_phone(phone):
    """驗證手機號碼是否為 09 開頭的 10 位數字"""
    if phone is None:
        return False
    return bool(PHONE_REGEX.match(str(phone).strip()))