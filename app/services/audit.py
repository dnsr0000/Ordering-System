from datetime import datetime

from flask import g, has_request_context, request
from flask_login import current_user

from app.extensions import db
from app.models.user import StaffAuditLog


# 稽核動作的顯示名稱與徽章樣式 (後台首頁的稽核分頁與 /admin/audit 共用；沒列的動作顯示代碼)
ACTION_LABELS = {
    'POS_CREATE_ORDER': ('POS 收銀建單', 'bg-warning text-dark'),
    'CANCEL_ORDER': ('取消訂單回滾', 'bg-danger'),
    'MARK_PICKED_UP': ('取餐核銷', 'bg-success'),
    'EDIT_USER': ('修改會員/點數', 'bg-info text-dark'),
    'POS_SET_MEMBER_PIN': ('櫃檯設定PIN', 'bg-info text-dark'),
    'DELETE_USER': ('刪除會員', 'bg-secondary'),
    'DELETE_MENU_ITEM': ('刪除菜單品項', 'bg-secondary'),
    'COUPON_CHANGE': ('優惠券異動', 'bg-secondary'),
    'COMBO_CHANGE': ('套餐異動', 'bg-secondary'),
    'UPDATE_REWARD_SETTING': ('紅利設定異動', 'bg-secondary'),
    'TOGGLE_USER_COUPON': ('會員券核銷切換', 'bg-secondary'),
    'TOGGLE_REWARD_ITEM': ('紅利商品上下架', 'bg-secondary'),
    'KITCHEN_COMPLETE_ALL': ('廚房一鍵出餐', 'bg-secondary'),
    'AUTO_CLOSE_STALE_ORDERS': ('換日自動結案', 'bg-secondary'),
    'STAFF_CREATE': ('新增員工帳號', 'bg-secondary'),
    'STAFF_TOGGLE_ACTIVE': ('員工帳號啟停', 'bg-secondary'),
    'STAFF_RESET_PASSWORD': ('重設員工密碼', 'bg-secondary'),
    'STAFF_DELETE_ACCOUNT': ('刪除員工帳號', 'bg-secondary'),
    'INTAKE_PAUSE': ('暫停接單', 'bg-danger'),
    'INTAKE_RESUME': ('恢復接單', 'bg-success'),
    'VOID_ITEM': ('單品作廢', 'bg-danger'),
    'REFUND': ('退款', 'bg-danger'),
    'MEMBER_VERIFY_OVERRIDE': ('主管核准免驗證會員', 'bg-warning text-dark'),
    'CASH_DRAWER_OPEN': ('手動開錢櫃', 'bg-warning text-dark'),
    'OFFLINE_ORDER': ('斷線暫存單補送', 'bg-secondary'),
    'MENU_PRO_CHANGE': ('菜單進階設定', 'bg-secondary'),
    'MENU_TRANSLATE': ('AI 翻譯菜單', 'bg-secondary'),
    'FEEDBACK_REPLY': ('回覆顧客評價', 'bg-info'),
    'SYSTEM_UPDATE': ('版本更新', 'bg-warning text-dark'),
    'SETTINGS_EXPORT': ('匯出系統設定', 'bg-secondary'),
    'SETTINGS_IMPORT': ('匯入系統設定', 'bg-warning text-dark'),
    'TENANT_TOGGLE': ('店家啟用 / 停用', 'bg-danger'),
    'AUDIT_EXPORT': ('匯出稽核紀錄', 'bg-secondary'),
}


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
