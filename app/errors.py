"""
統一錯誤處理：
- 商業邏輯以 raise AppError(訊息, 狀態碼) 回報錯誤，不再在每個函式回傳 (dict, code)。
- API 請求一律回 {"success": false, "message": ...} JSON；
  表單請求則以 flash 訊息顯示並導回原頁面 (Post/Redirect/Get)。
"""
import logging
from urllib.parse import urlparse

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)

# 不在 /api/ 底下、但由前端以 fetch 呼叫並期待 JSON 的端點
JSON_ENDPOINTS = {'/phone_login', '/submit_order', '/face_login', '/my_orders'}

HTTP_MESSAGES = {
    400: '請求格式不正確。',
    401: '請先登入。',
    403: '權限不足，無法執行此操作。',
    404: '查無此資料。',
    405: '不支援此請求方式。',
    413: '上傳內容超出系統限制 (上限 50MB)！',
    429: '請求過於頻繁，請稍後再試！',
    500: '系統發生錯誤，請稍後再試。',
}


class AppError(Exception):
    """可預期的業務錯誤。extra 會併入 JSON 回應 (例如 insufficient_stock)。"""

    def __init__(self, message, status=400, redirect_to=None, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.redirect_to = redirect_to
        self.extra = extra


def wants_json():
    return (
        request.is_json
        or request.path.startswith('/api/')
        or request.path in JSON_ENDPOINTS
        or request.accept_mimetypes.best == 'application/json'
    )


def json_error(message, status, **extra):
    resp = jsonify({'success': False, 'message': message, **extra})
    resp.status_code = status
    return resp


def _safe_back_url(default):
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return ref
    return default


def register_error_handlers(app):
    @app.errorhandler(AppError)
    def handle_app_error(err):
        from app.extensions import db
        db.session.rollback()  # 業務錯誤一律放棄這次請求尚未提交的異動
        if wants_json():
            return json_error(err.message, err.status, **err.extra)
        if err.status not in (400, 409):  # 權限不足、查無資料等：顯示錯誤頁並保留狀態碼
            return render_template('error.html', status=err.status, message=err.message), err.status
        # 表單驗證失敗：以提示訊息導回原頁面 (Post/Redirect/Get)
        flash(err.message, 'danger')
        default = url_for('admin.admin_dashboard') if request.blueprint == 'admin' else url_for('customer.customer_index')
        return redirect(err.redirect_to or _safe_back_url(default))

    @app.errorhandler(CSRFError)
    def handle_csrf_error(err):
        msg = '安全驗證失敗 (CSRF Token 無效或已過期)，請重新整理頁面後再試！'
        if wants_json():
            return json_error(msg, 403)
        return render_template('error.html', status=403, message=msg), 403

    @app.errorhandler(HTTPException)
    def handle_http_error(err):
        status = err.code or 500
        message = HTTP_MESSAGES.get(status, err.description)
        if wants_json():
            resp = json_error(message, status)
        else:
            resp = render_template('error.html', status=status, message=message), status
        # 保留 429 的 Retry-After 等標頭
        if status == 429 and hasattr(err, 'get_headers'):
            from flask import make_response
            resp = make_response(resp)
            for k, v in err.get_headers():
                if k.lower() != 'content-type':
                    resp.headers[k] = v
        return resp

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        logger.exception('未預期的錯誤: %s %s', request.method, request.path)
        from app.extensions import db
        db.session.rollback()
        if wants_json():
            return json_error(HTTP_MESSAGES[500], 500)
        return render_template('error.html', status=500, message=HTTP_MESSAGES[500]), 500
