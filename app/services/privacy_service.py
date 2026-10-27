"""
個人資料保護：隱私權告知的內容資料、同意紀錄、撤回人臉辨識、刪除長期未使用的會員。
告知頁 templates/privacy.html 依個資法第 8 條列出應告知事項；營運者名稱與聯絡方式在後台設定。
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, select, update

from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.sync import PointLedger
from app.models.tenant import Tenant
from app.models.user import PrivacySetting, User
from app.services.upload_paths import remove_upload

# 告知內容有實質修改時更新版本：會員下次登入會再次請他確認
NOTICE_VERSION = '2026-10-23'   # 新增：生日、LINE、簡訊驗證、儲值金、訂位候位、電子發票


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


# ---------------------------------------------------------------------------
# 會員自助：下載個人資料、刪除帳號 (個資法第 3 條：查詢、閱覽、製給複製本、請求刪除)
# ---------------------------------------------------------------------------
def export_member_data(user):
    """會員本人的資料 (JSON 可序列化)：基本資料、訂單、點數 / 儲值金 / 集章異動、優惠券"""
    from app.models.coupon import UserCoupon
    from app.models.member import StampLedger, WalletLedger

    def ts(v):
        return v.isoformat(sep=' ', timespec='seconds') if isinstance(v, datetime) else (v.isoformat() if v else None)

    orders = Order.query.filter_by(user_id=user.id).order_by(Order.id).all()
    return {
        'exported_at': ts(datetime.now()),
        'profile': {
            'name': user.name, 'phone': user.phone, 'birthday': ts(user.birthday),
            'points': user.points or 0, 'wallet_balance': user.wallet_balance or 0,
            'tier': user.tier.name if user.tier else '', 'line_bound': bool(user.line_user_id),
            'face_login': user.feature is not None, 'privacy_consent_version': user.privacy_consent_version,
            'privacy_consented_at': ts(user.privacy_consented_at), 'face_consented_at': ts(user.face_consented_at),
            'phone_verified_at': ts(user.phone_verified_at), 'last_login_at': ts(user.last_login_at),
            'referral_code': user.referral_code,
        },
        'orders': [{'order_id': o.id, 'created_at': ts(o.created_at), 'status': o.status_label, 'total': o.total_price,
                    'payment_method': o.payment_method, 'order_type': o.order_type,
                    'points_earned': o.points_earned or 0, 'points_used': o.points_used or 0,
                    'items': [{'name': i.item_name, 'quantity': i.quantity, 'price': i.price,
                               'customization': i.customization or ''} for i in o.items]} for o in orders],
        'point_ledger': [{'at': ts(p.created_at), 'delta': p.delta, 'reason': p.reason}
                         for p in PointLedger.query.filter_by(user_id=user.id).order_by(PointLedger.id).all()],
        'wallet_ledger': [{'at': ts(w.created_at), 'delta': w.delta, 'reason': w.reason, 'note': w.note}
                          for w in WalletLedger.query.filter_by(user_id=user.id).order_by(WalletLedger.id).all()],
        'stamp_ledger': [{'at': ts(s.created_at), 'delta': s.delta, 'reason': s.reason}
                         for s in StampLedger.query.filter_by(user_id=user.id).order_by(StampLedger.id).all()],
        'coupons': [{'code': c.code, 'title': c.coupon.title if c.coupon else '', 'used': bool(c.is_used),
                     'received_at': ts(c.created_at), 'expires_at': ts(c.expires_at), 'used_at': ts(c.used_at)}
                    for c in UserCoupon.query.filter_by(user_id=user.id).order_by(UserCoupon.id).all()],
    }


def delete_member_account(user, reason='會員自行刪除帳號'):
    """
    會員刪除帳號：人臉資料與照片刪除、訂單保留但去識別化 (帳務需保存)、會員資料刪除 (同步到各分店與線上端)。
    儲值金尚有餘額時不可刪除 (需先退費或用完)。由呼叫端 commit 前已處理好登出。
    """
    from app.services.audit import record_staff_audit
    if (user.wallet_balance or 0) > 0:
        raise AppError(f'儲值金尚有 ${user.wallet_balance} 餘額，請先使用完畢或洽櫃檯辦理退費後再刪除帳號', 409)
    withdraw_face(user)
    db.session.execute(update(Order).where(Order.user_id == user.id).values(table_number='已刪除會員')
                       .execution_options(synchronize_session=False))
    record_staff_audit('MEMBER_SELF_DELETE', target_user_id=user.id, tenant_id=user.tenant_id, as_system=True,
                       details=f'{reason} (會員 #{user.id})')
    db.session.delete(user)
