from datetime import datetime
from flask import session, request, g
from sqlalchemy.exc import IntegrityError
from app.extensions import db
from app.models.user import CustomerLog, User, StaffAuditLog, RewardRedemption, RewardSetting
from app.services.voucher_service import list_open_vouchers, try_refund_voucher

def start_customer_session(user_type, identifier):
    close_customer_session()
    log = CustomerLog(
        user_type=user_type,
        identifier=identifier,
        login_at=datetime.now(),
        ip_address=request.remote_addr or ''
    )
    db.session.add(log)
    db.session.commit()
    session['customer_log_id'] = log.id
    return log.id


def clear_customer_session():
    for key in ['user_id', 'user_name', 'user_points', 'is_guest', 'customer_log_id']:
        session.pop(key, None)

def refund_orphaned_redemptions(user):
    """
    檢查並自動退還尚未送單結帳 (order_id is None) 的紅利商品點數，防止吃點數
    """
    if not user:
        return 0
    tenant_id = user.tenant_id
    total_refund = 0
    # 逐張以原子條件式 UPDATE 退點：只有「這次呼叫真的把憑證從未使用改成已退點」才加回點數，
    # 與結帳核銷、手動退點並行時不會重複退或與核銷同時成立
    for voucher_id, cost in list_open_vouchers(tenant_id, user.id):
        if try_refund_voucher(tenant_id, user.id, voucher_id):
            total_refund += cost
    if total_refund > 0:
        db.session.execute(
            db.text("UPDATE user SET points = points + :pts WHERE id = :id AND tenant_id = :t"),
            {"pts": total_refund, "id": user.id, "t": tenant_id}
        )
        db.session.commit()
        return total_refund
    return 0

def close_customer_session():
    log_id = session.get('customer_log_id')
    if log_id:
        log = db.session.get(CustomerLog, log_id)
        if log and not log.logout_at:
            log.logout_at = datetime.now()
    user_id = session.get('user_id')
    if user_id:
        user = db.session.get(User, user_id)
        if user:
            user.last_logout_at = datetime.now()
            refund_orphaned_redemptions(user)
    db.session.commit()
    session.pop('customer_log_id', None)


def get_or_create_reward_setting(tenant_id=None):
    """
    取得目前店家的紅利設定，沒有時建立預設值。
    reward_setting.tenant_id 具備唯一索引：多個請求同時第一次進來時，只有一個建立成功，
    其餘遇到 IntegrityError 就改讀已存在的那一筆，不會產生重複設定列。
    """
    setting = RewardSetting.query.first()
    if setting is not None:
        return setting
    tid = tenant_id or getattr(g, 'tenant_id', None) or session.get('tenant_id')
    try:
        with db.session.begin_nested():
            setting = RewardSetting(
                tenant_id=tid, is_enabled=True, points_per_dollar=1,
                max_discount_per_order=0, spend_per_point=100
            )
            db.session.add(setting)
            db.session.flush()
    except IntegrityError:
        setting = RewardSetting.query.first()
    db.session.commit()
    return setting


def record_staff_audit(action_type, target_order_id=None, target_user_id=None, points_delta=0, details='', tenant_id=None):
    staff = getattr(g, 'staff_account', None)
    staff_id = getattr(staff, 'id', None) or session.get('staff_account_id')
    staff_username = getattr(staff, 'username', None) or session.get('admin_username') or 'system'
    staff_role = getattr(staff, 'role', None) or session.get('admin_role') or session.get('staff_role') or 'staff'
    # 稽核紀錄一定要歸屬明確的店家；沒有租戶資訊時直接報錯，不再「猜」成 1 號店而把紀錄記到別家
    tenant_id = tenant_id or getattr(g, 'tenant_id', None) or session.get('tenant_id')
    if tenant_id is None:
        raise RuntimeError('缺少租戶資訊，無法寫入員工稽核紀錄')

    audit = StaffAuditLog(
        tenant_id=tenant_id,
        staff_account_id=staff_id,
        staff_username=staff_username,
        staff_role=staff_role,
        action_type=action_type,
        target_order_id=target_order_id,
        target_user_id=target_user_id,
        points_delta=int(points_delta or 0),
        details=str(details or '')[:500],
        ip_address=request.remote_addr or '',
        created_at=datetime.now()
    )
    # 資料表缺欄位這類問題由啟動時與還原後的遷移負責修復；這裡不再於請求途中執行遷移
    # (遷移內的 commit/rollback 會把呼叫端尚未完成的交易提前提交)
    db.session.add(audit)
    return audit
