from datetime import datetime

from flask import g, has_request_context, request
from flask_login import current_user

from app.extensions import db
from app.models.user import StaffAuditLog


def record_staff_audit(action_type, target_order_id=None, target_user_id=None, points_delta=0, details='',
                       tenant_id=None, as_system=False):
    """
    寫入員工操作稽核 (由呼叫端 commit，與業務異動同一交易)。
    系統工作 (as_system=True 或非請求情境) 記為 system；店家必須明確，不可猜測。
    """
    as_system = as_system or not has_request_context()
    staff = None if as_system or not current_user.is_authenticated else current_user
    tenant_id = tenant_id or (getattr(g, 'tenant_id', None) if has_request_context() else None)
    if tenant_id is None:
        raise RuntimeError('缺少店家資訊，無法寫入員工稽核紀錄')
    audit = StaffAuditLog(
        tenant_id=tenant_id,
        staff_account_id=staff.id if staff else None,
        staff_username=staff.username if staff else 'system',
        staff_role=staff.role if staff else 'system',
        action_type=action_type,
        target_order_id=target_order_id,
        target_user_id=target_user_id,
        points_delta=int(points_delta or 0),
        details=str(details or '')[:500],
        ip_address=(request.remote_addr or '') if has_request_context() else '',
        created_at=datetime.now(),
    )
    db.session.add(audit)
    return audit
