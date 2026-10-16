from datetime import datetime
from flask import session, request, g
from app.extensions import db
from app.models.user import CustomerLog, User, StaffAuditLog


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
    db.session.commit()
    session.pop('customer_log_id', None)


def record_staff_audit(action_type, target_order_id=None, target_user_id=None, points_delta=0, details=''):
    staff = getattr(g, 'staff_account', None)
    staff_id = getattr(staff, 'id', None) or session.get('staff_account_id')
    staff_username = getattr(staff, 'username', None) or session.get('admin_username') or 'system'
    staff_role = getattr(staff, 'role', None) or session.get('admin_role') or session.get('staff_role') or 'staff'
    tenant_id = getattr(g, 'tenant_id', None) or session.get('tenant_id') or 1

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
    try:
        with db.session.begin_nested():
            db.session.add(audit)
            db.session.flush()
    except Exception:
        from app.services.migration_service import run_database_migrations
        run_database_migrations()
        db.session.add(audit)
    return audit
