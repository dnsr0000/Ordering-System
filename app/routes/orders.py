from flask import Blueprint, g, jsonify
from flask_login import current_user

from app.models.enums import ALL_STAFF_ROLES
from app.services import approval_service
from app.services.order_service import change_order_status
from app.utils import get_json_dict, role_required

orders_bp = Blueprint('orders', __name__)


@orders_bp.route('/api/orders/<int:order_id>/status', methods=['POST'])
@role_required(*ALL_STAFF_ROLES)
def api_update_order_status(order_id):
    """
    全系統唯一的訂單狀態更新端點 (廚房、取餐、POS、後台共用)。
    狀態流轉是否合法、角色權限與併發互斥皆由 order_service.change_order_status 判定
    (403 無權限 / 404 查無 / 409 非法或已被變更)。
    """
    data = get_json_dict()
    order = change_order_status(order_id, data.get('status'), current_user, g.tenant_id,
                                approve=lambda perm, label: approval_service.require_approval(perm, data, label),
                                reason=str(data.get('reason') or '').strip()[:200])
    return jsonify({'success': True, 'order_id': order.id, 'status': order.status,
                    'message': f'訂單狀態已成功更新為【{order.status}】'})
