import os
import time
import json
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


def safe_int(val, default=0):
    try:
        if val is None or val == '':
            return default
        return int(float(val))
    except (ValueError, TypeError):
        return default


def js_alert_redirect(msg, target_url=None, status=200):
    safe_msg = json.dumps(str(msg), ensure_ascii=False)
    if target_url:
        safe_url = json.dumps(str(target_url), ensure_ascii=False)
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
            b_key = f"ratelimit:{key_prefix}:{tid}:{ip}"
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
