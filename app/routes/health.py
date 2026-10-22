"""
健康檢查 (容器 HEALTHCHECK、反向代理、監控服務使用)：
GET /healthz 不需登入、不受頻率限制，只檢查資料庫是否可連線。
正常回 200 {"status": "ok"}；資料庫無法連線回 503 (監控端據此判斷重啟或告警)。
"""
import logging

from flask import Blueprint, jsonify
from sqlalchemy import text

from app.extensions import csrf, db, limiter

health_bp = Blueprint('health', __name__)
logger = logging.getLogger(__name__)


@health_bp.route('/healthz', methods=['GET', 'HEAD'])
@limiter.exempt
@csrf.exempt
def healthz():
    try:
        db.session.execute(text('SELECT 1'))
        database = 'ok'
    except Exception as e:  # noqa: BLE001  (任何資料庫錯誤都視為不健康，不讓例外變成 500)
        db.session.rollback()
        logger.warning('健康檢查：資料庫無法連線 (%s)', e.__class__.__name__)
        database = 'unavailable'
    healthy = database == 'ok'
    resp = jsonify({'status': 'ok' if healthy else 'error', 'database': database})
    resp.status_code = 200 if healthy else 503
    resp.headers['Cache-Control'] = 'no-store'
    return resp
