"""One-time contact verification for optional-contact tenant owner recovery."""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import update
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.errors import AppError
from app.models.tenant import TenantRecoveryCode
from app.services import mail_service, sms_service

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
RESEND_AFTER = timedelta(seconds=60)
MAX_CODES_PER_HOUR = 5
MAX_CODE_ATTEMPTS = 5


def contact_value(tenant, channel):
    value = tenant.contact_email if channel == 'email' else tenant.contact_phone if channel == 'sms' else ''
    value = str(value or '').strip()
    return value.lower() if channel == 'email' else value


def _contact_hash(channel, value):
    return hashlib.sha256(f'{channel}\0{value}'.encode('utf-8')).hexdigest()


def _channel_ready(tenant_id, channel):
    return mail_service.is_configured(tenant_id) if channel == 'email' else sms_service.is_enabled(tenant_id)


def _email_sender_tenant_id(tenant_id):
    if mail_service.is_configured(tenant_id):
        return tenant_id
    from app.models.tenant import Tenant
    from app.tenant_context import tenant_bypass
    with tenant_bypass():
        platform_tenant = Tenant.query.filter_by(slug='default').first()
    if platform_tenant is not None and platform_tenant.id != tenant_id \
            and mail_service.is_configured(platform_tenant.id):
        return platform_tenant.id
    return None


def send_code(tenant, staff, purpose, channel, ip=''):
    """Send a rate-limited code; return False when contact or delivery channel is unavailable."""
    if purpose not in ('reset_password', 'reactivate') or channel not in ('email', 'sms'):
        return False
    destination = contact_value(tenant, channel)
    sender_tenant_id = _email_sender_tenant_id(tenant.id) if channel == 'email' else tenant.id
    channel_ready = sender_tenant_id is not None if channel == 'email' else _channel_ready(tenant.id, channel)
    if not destination or not channel_ready:
        return False

    now = datetime.now()
    recent = (TenantRecoveryCode.query.filter_by(tenant_id=tenant.id, staff_account_id=staff.id,
                                                  purpose=purpose, channel=channel)
              .filter(TenantRecoveryCode.created_at >= now - timedelta(hours=1))
              .order_by(TenantRecoveryCode.created_at.desc(), TenantRecoveryCode.id.desc()).all())
    if len(recent) >= MAX_CODES_PER_HOUR or (recent and now - recent[0].created_at < RESEND_AFTER):
        return False

    code = f'{secrets.randbelow(1_000_000):06d}'
    db.session.execute(
        update(TenantRecoveryCode).where(TenantRecoveryCode.tenant_id == tenant.id,
                                         TenantRecoveryCode.staff_account_id == staff.id,
                                         TenantRecoveryCode.purpose == purpose,
                                         TenantRecoveryCode.channel == channel,
                                         TenantRecoveryCode.consumed_at.is_(None))
        .values(consumed_at=now).execution_options(synchronize_session=False))
    recovery = TenantRecoveryCode(tenant_id=tenant.id, staff_account_id=staff.id, purpose=purpose, channel=channel,
                                  contact_hash=_contact_hash(channel, destination),
                                  code_hash=generate_password_hash(code), expires_at=now + CODE_TTL,
                                  ip_address=(ip or '')[:50])
    db.session.add(recovery)
    db.session.commit()
    body = f'【{tenant.name}】帳號復原驗證碼：{code}，10 分鐘內有效，請勿提供給他人。'
    try:
        if channel == 'email':
            mail_service.send_mail(sender_tenant_id, [destination], f'{tenant.name} 帳號復原驗證碼', body)
        else:
            sms_service.send_sms(tenant.id, destination, body)
    except AppError as exc:
        recovery.consumed_at = datetime.now()
        db.session.commit()
        logger.warning('店家帳號復原驗證碼發送失敗 (tenant=%s, channel=%s)：%s', tenant.id, channel, exc.message)
        return False
    return True


def verify_code(tenant, staff, purpose, channel, code):
    """Count an attempt before comparing; return an unconsumed code row only on success."""
    destination = contact_value(tenant, channel)
    if not destination:
        return None
    now = datetime.now()
    recovery = (TenantRecoveryCode.query.filter_by(
        tenant_id=tenant.id, staff_account_id=staff.id, purpose=purpose, channel=channel,
        contact_hash=_contact_hash(channel, destination),
    ).filter(TenantRecoveryCode.consumed_at.is_(None), TenantRecoveryCode.expires_at >= now,
             TenantRecoveryCode.attempts < MAX_CODE_ATTEMPTS)
               .order_by(TenantRecoveryCode.created_at.desc(), TenantRecoveryCode.id.desc()).first())
    if recovery is None:
        return None
    counted = db.session.execute(
        update(TenantRecoveryCode).where(TenantRecoveryCode.id == recovery.id,
                                         TenantRecoveryCode.consumed_at.is_(None),
                                         TenantRecoveryCode.expires_at >= now,
                                         TenantRecoveryCode.attempts < MAX_CODE_ATTEMPTS)
        .values(attempts=TenantRecoveryCode.attempts + 1)
        .execution_options(synchronize_session=False)).rowcount
    db.session.commit()
    if not counted:
        return None
    db.session.refresh(recovery)
    return recovery if check_password_hash(recovery.code_hash, str(code or '').strip()) else None


def consume_code(recovery):
    now = datetime.now()
    return db.session.execute(
        update(TenantRecoveryCode).where(TenantRecoveryCode.id == recovery.id,
                                         TenantRecoveryCode.consumed_at.is_(None),
                                         TenantRecoveryCode.expires_at >= now)
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)).rowcount == 1