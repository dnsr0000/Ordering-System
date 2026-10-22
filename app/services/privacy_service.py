"""
個人資料保護：隱私權告知的內容資料、同意紀錄、撤回人臉辨識、刪除長期未使用的會員。
告知頁 templates/privacy.html 依個資法第 8 條列出應告知事項；營運者名稱與聯絡方式在後台設定。
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, select, update

from app.extensions import db
from app.models.order import Order
from app.models.sync import PointLedger
from app.models.tenant import Tenant
from app.models.user import PrivacySetting, User
from app.services.upload_paths import remove_upload

# 告知內容有實質修改時更新版本：會員下次登入會再次請他確認
NOTICE_VERSION = '2026-10-22'


def get_setting(tenant_id):
    return PrivacySetting.query.filter_by(tenant_id=tenant_id).first()


def notice_context(tenant_id):
    """告知頁需要的營運者資訊；未設定時以店家名稱代入"""
    setting = get_setting(tenant_id)
    tenant = db.session.get(Tenant, tenant_id)
    return {
        'operator_name': (setting.operator_name if setting and setting.operator_name else (tenant.name if tenant else '本店')),
        'contact_email': setting.contact_email if setting else '',
        'contact_phone': setting.contact_phone if setting else '',
        'contact_address': setting.contact_address if setting else '',
        'retention_months': setting.retention_months if setting else 24,
        'version': NOTICE_VERSION,
        'configured': bool(setting and setting.operator_name and (setting.contact_email or setting.contact_phone)),
    }


def save_setting(tenant_id, form):
    setting = get_setting(tenant_id) or PrivacySetting(tenant_id=tenant_id)
    setting.operator_name = (form.get('operator_name') or '').strip()[:120]
    setting.contact_email = (form.get('contact_email') or '').strip()[:120]
    setting.contact_phone = (form.get('contact_phone') or '').strip()[:50]
    setting.contact_address = (form.get('contact_address') or '').strip()[:200]
    try:
        months = int(form.get('retention_months') or 24)
    except ValueError:
        months = 24
    setting.retention_months = max(6, min(120, months))
    db.session.add(setting)
    db.session.commit()
    return setting


def record_consent(user, face=False):
    now = datetime.now()
    user.privacy_consent_version = NOTICE_VERSION
    user.privacy_consented_at = now
    if face:
        user.face_consented_at = now


def needs_consent(user):
    return user is not None and user.privacy_consent_version != NOTICE_VERSION


def withdraw_face(user):
    """撤回人臉辨識同意：刪除特徵值與照片 (其他分店與線上端經同步一併刪除特徵值)"""
    if user.photo_path:
        remove_upload(current_app.config['UPLOAD_FOLDER_MEMBER'], user.photo_path)
    user.feature = None
    user.photo_path = ''
    user.face_consented_at = None


def inactive_members(tenant_id, months, now=None):
    """最後一次登入、消費、點數異動都早於 months 個月前的會員"""
    cutoff = (now or datetime.now()) - timedelta(days=30 * months)
    last_order = select(func.max(Order.created_at)).where(Order.user_id == User.id).scalar_subquery()
    last_points = select(func.max(PointLedger.created_at)).where(PointLedger.user_id == User.id).scalar_subquery()
    stale = []
    for user, order_at, points_at in db.session.execute(
            select(User, last_order, last_points).where(User.tenant_id == tenant_id)).all():
        activity = [t for t in (user.last_login_at, order_at, points_at, user.privacy_consented_at) if t]
        if not activity or max(activity) < cutoff:
            stale.append(user)
    return stale


def purge_inactive_members(tenant_id, months, dry_run=True):
    """刪除長期未使用的會員 (訂單保留但去識別化)；回傳被刪除 (或將被刪除) 的會員清單"""
    stale = inactive_members(tenant_id, months)
    if dry_run:
        return stale
    from app.services.audit import record_staff_audit
    for user in stale:
        withdraw_face(user)
        db.session.execute(update(Order).where(Order.user_id == user.id).values(table_number='已刪除會員')
                           .execution_options(synchronize_session=False))
        record_staff_audit('PRIVACY_PURGE', target_user_id=user.id, tenant_id=tenant_id, as_system=True,
                           details=f'會員停止使用超過 {months} 個月，依隱私權告知刪除 (會員 #{user.id})')
        db.session.delete(user)
    db.session.commit()
    return stale
