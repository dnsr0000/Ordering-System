"""
外送員手機頁 (免登入，憑訂單上的 driver_token)：顯示配送資料與導航連結，允許定位後定期回報位置；
顧客追蹤頁顯示外送員最新位置。憑證在派車頁指派外送員時產生 (delivery_tracking_service)。
"""
from flask import Blueprint, abort, jsonify, render_template

from app.extensions import csrf, db, limiter
from app.models.tenant import Tenant
from app.services import delivery_tracking_service as tracking
from app.tenant_context import tenant_bypass
from app.utils import get_json_dict

driver_bp = Blueprint('driver', __name__)


def _order_or_404(token):
    order = tracking.find_order(token)
    if order is None or not tracking.enabled_for_order(order):
        abort(404)
    return order


@driver_bp.route('/driver/<token>', endpoint='page')
@limiter.limit('60/minute')
def page(token):
    order = _order_or_404(token)
    with tenant_bypass():
        shop = db.session.get(Tenant, order.tenant_id)
        data = dict(tracking.driver_payload(order), token=order.driver_token)
    return render_template('driver.html', order=order, shop=shop, data=data)


@driver_bp.route('/api/driver/<token>', endpoint='status')
@limiter.limit('120/minute')
def status(token):
    order = _order_or_404(token)
    with tenant_bypass():
        return jsonify(dict(tracking.driver_payload(order), success=True))


@driver_bp.route('/api/driver/<token>/position', methods=['POST'], endpoint='position')
@csrf.exempt
@limiter.limit('120/minute')
def position(token):
    """外送員手機回報位置 {lat, lng, accuracy}；配送結束後回 409 (手機端停止回報)。
    只憑網址裡的 driver_token 授權、不依賴 cookie 身分，所以免 CSRF：外送員是匿名 session，
    頁面開著超過 session 期限 (30 分鐘) 才開始回報時，session 裡的 csrf_token 已失效，否則每次都 403"""
    order = _order_or_404(token)
    data = get_json_dict()
    with tenant_bypass():
        pos = tracking.record_position(order, data.get('lat'), data.get('lng'), data.get('accuracy'),
                                       data.get('age'))
        db.session.commit()
        return jsonify({'success': True, 'at': pos.to_dict()['at'], 'delivery_status': order.delivery_status,
                        'active': tracking.is_active(order), 'interval': tracking.interval_seconds(order.tenant_id)})
