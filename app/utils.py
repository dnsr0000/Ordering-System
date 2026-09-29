# 檔案位置：app/decorators.py
from functools import wraps
from flask import session, redirect, url_for, request, jsonify

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in') or not session.get('staff_account_id') or not session.get('tenant_id'):
            # 若為 API 請求回傳 JSON 401，一般網頁轉跳回登入頁
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': '未授權操作，請先登入管理員！'}), 401
            return redirect(url_for('admin.admin_dashboard'))
        return f(*args, **kwargs)
    return decorated_function