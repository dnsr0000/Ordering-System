from flask import Blueprint, request, jsonify, g
from app.services.order_service import change_order_status
from app.utils import role_required

orders_bp = Blueprint('orders', __name__)


@orders_bp.route('/api/orders/<int:order_id>/status', methods=['POST'])
@role_required('owner', 'manager', 'staff', 'kitchen')
def api_update_order_status(order_id):
    """
    全系統唯一正式訂單狀態更新端點 (廚房 KDS、櫃檯取餐、POS、後台共用)：
    路由層僅做登入與基本角色把關，狀態流轉合法性、角色轉換權限與併發互斥皆由
    order_service.change_order_status 統一判定 (403 無權限 / 404 查無 / 409 非法或已被變更)。
    """
    data = request.get_json(silent=True) or {}
    result, status_code = change_order_status(
        order_id=order_id,
        target_status=data.get('status'),
        staff_account=g.staff_account,
        tenant_id=g.tenant_id
    )
    return jsonify(result), status_code
