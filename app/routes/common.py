from datetime import datetime
from flask import session, request
from app.extensions import db
from app.models.user import CustomerLog, User

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