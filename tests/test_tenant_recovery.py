"""Optional-contact owner password recovery and self-closure reactivation."""
import re
import uuid
from datetime import datetime, timedelta

from app.extensions import db
from app.models.tenant import StaffAccount, Tenant, TenantClosureRequest, TenantRecoveryCode
from app.services import mail_service, sms_service
from tests.support import get_or_create_tenant, login_staff


def _recovery_code(message):
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', message)
    assert match, message
    return match.group(1)


def test_owner_resets_password_after_email_code(app, monkeypatch):
    slug = f'recover-email-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    username = f'owner-{slug}'
    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        tenant.contact_email = 'owner@example.test'
        db.session.commit()
    old_session = login_staff(app, username, role='owner', tenant_id=tenant_id)
    sent = []
    monkeypatch.setattr(mail_service, 'is_configured', lambda tenant_id=None: True)
    monkeypatch.setattr(mail_service, 'send_mail', lambda tid, recipients, subject, body, attachments=():
                        sent.append((tid, recipients, subject, body)))

    client = app.test_client()
    started = client.post('/tenant/recovery/send', data={
        'slug': slug, 'username': username, 'purpose': 'reset_password', 'channel': 'email',
    }, follow_redirects=True)
    assert started.status_code == 200 and '若資料相符' in started.get_data(as_text=True)
    assert len(sent) == 1 and sent[0][1] == ['owner@example.test']
    code = _recovery_code(sent[0][3])
    with app.app_context():
        recovery = TenantRecoveryCode.query.filter_by(tenant_id=tenant_id, staff_account_id=
                                                       StaffAccount.query.filter_by(username=username).one().id).one()
        assert recovery.code_hash != code and recovery.contact_hash

    result = client.post('/tenant/recovery/complete', data={
        'slug': slug, 'username': username, 'purpose': 'reset_password', 'channel': 'email',
        'code': code, 'new_password': 'RecoveredPassword!2026', 'confirm_password': 'RecoveredPassword!2026',
    })
    assert result.status_code == 302
    assert client.get('/admin').status_code == 200
    old_session_response = old_session.get('/admin')
    assert old_session_response.status_code == 200 and '管理員登入' in old_session_response.get_data(as_text=True)
    with app.app_context():
        owner = StaffAccount.query.filter_by(username=username).one()
        assert owner.check_password('RecoveredPassword!2026')
        assert TenantRecoveryCode.query.filter_by(tenant_id=tenant_id).one().consumed_at is not None


def test_owner_reactivates_self_closed_tenant_with_sms_code(app, monkeypatch):
    slug = f'recover-reactivate-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    username = f'owner-{slug}'
    owner_client = login_staff(app, username, role='owner', tenant_id=tenant_id)
    sent = []
    monkeypatch.setattr(sms_service, 'is_enabled', lambda tenant_id=None: True)
    monkeypatch.setattr(sms_service, 'send_sms', lambda tid, phone, message: sent.append((tid, phone, message)))
    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        tenant.contact_phone = '+886 912-345-678'
        tenant.is_active = False
        owner = StaffAccount.query.filter_by(username=username).one()
        closure = TenantClosureRequest(tenant_id=tenant_id, requested_by_staff_id=owner.id,
                                       requested_by_username=username, reason='測試自助恢復')
        db.session.add(closure)
        db.session.commit()

    client = app.test_client()
    started = client.post('/tenant/recovery/send', data={
        'slug': slug, 'username': username, 'purpose': 'reactivate', 'channel': 'sms',
    }, follow_redirects=True)
    assert started.status_code == 200 and '若資料相符' in started.get_data(as_text=True)
    assert len(sent) == 1 and sent[0][1] == '+886 912-345-678'
    code = _recovery_code(sent[0][2])
    result = client.post('/tenant/recovery/complete', data={
        'slug': slug, 'username': username, 'purpose': 'reactivate', 'channel': 'sms',
        'code': code, 'new_password': 'ReactivatedPassword!2026', 'confirm_password': 'ReactivatedPassword!2026',
    })
    assert result.status_code == 302
    assert client.get('/admin').status_code == 200
    old_session_response = owner_client.get('/admin')
    assert old_session_response.status_code == 200 and '管理員登入' in old_session_response.get_data(as_text=True)
    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        closure = TenantClosureRequest.query.filter_by(tenant_id=tenant_id).one()
        owner = StaffAccount.query.filter_by(username=username).one()
        assert tenant.is_active and closure.status == 'owner_reactivated'
        assert owner.check_password('ReactivatedPassword!2026')


def test_recovery_does_not_send_without_contact_or_for_platform_suspension(app, monkeypatch):
    slug = f'recover-no-contact-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    username = f'owner-{slug}'
    login_staff(app, username, role='owner', tenant_id=tenant_id)
    sms_sent, email_sent = [], []
    monkeypatch.setattr(sms_service, 'is_enabled', lambda tenant_id=None: True)
    monkeypatch.setattr(sms_service, 'send_sms', lambda *args: sms_sent.append(args))
    monkeypatch.setattr(mail_service, 'is_configured', lambda tenant_id=None: True)
    monkeypatch.setattr(mail_service, 'send_mail', lambda *args, **kwargs: email_sent.append(args))
    client = app.test_client()

    no_contact = client.post('/tenant/recovery/send', data={
        'slug': slug, 'username': username, 'purpose': 'reset_password', 'channel': 'email',
    }, follow_redirects=True)
    assert no_contact.status_code == 200 and '若資料相符' in no_contact.get_data(as_text=True)
    assert sms_sent == [] and email_sent == []

    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        tenant.contact_phone = '0912345678'
        tenant.is_active = False  # Platform suspension has no owner closure request.
        db.session.commit()
    manual_suspend = client.post('/tenant/recovery/send', data={
        'slug': slug, 'username': username, 'purpose': 'reactivate', 'channel': 'sms',
    }, follow_redirects=True)
    assert manual_suspend.status_code == 200 and '若資料相符' in manual_suspend.get_data(as_text=True)
    assert sms_sent == [] and email_sent == []
    with app.app_context():
        assert db.session.get(Tenant, tenant_id).is_active is False
        assert TenantRecoveryCode.query.filter_by(tenant_id=tenant_id).count() == 0


def test_sending_new_recovery_code_invalidates_previous_code(app, monkeypatch):
    slug = f'recover-resend-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    username = f'owner-{slug}'
    login_staff(app, username, role='owner', tenant_id=tenant_id)
    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        tenant.contact_email = 'resend@example.test'
        db.session.commit()
    sent = []
    monkeypatch.setattr(mail_service, 'is_configured', lambda tenant_id=None: True)
    monkeypatch.setattr(mail_service, 'send_mail', lambda tid, recipients, subject, body, attachments=():
                        sent.append(body))
    client = app.test_client()
    request_data = {'slug': slug, 'username': username, 'purpose': 'reset_password', 'channel': 'email'}
    client.post('/tenant/recovery/send', data=request_data)
    old_code = _recovery_code(sent[-1])
    with app.app_context():
        recovery = TenantRecoveryCode.query.filter_by(tenant_id=tenant_id).one()
        recovery.created_at = datetime.now() - timedelta(seconds=61)
        db.session.commit()

    client.post('/tenant/recovery/send', data=request_data)
    new_code = _recovery_code(sent[-1])
    assert new_code != old_code
    from app.services import tenant_recovery_service
    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        owner = StaffAccount.query.filter_by(tenant_id=tenant_id, username=username).one()
        assert tenant_recovery_service.verify_code(tenant, owner, 'reset_password', 'email', old_code) is None
        assert tenant_recovery_service.verify_code(tenant, owner, 'reset_password', 'email', new_code) is not None
        codes = TenantRecoveryCode.query.filter_by(tenant_id=tenant_id).order_by(TenantRecoveryCode.id).all()
        assert codes[0].consumed_at is not None
