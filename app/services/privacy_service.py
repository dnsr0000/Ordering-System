"""
個人資料保護：隱私權告知的內容資料、同意紀錄、撤回人臉辨識、刪除長期未使用的會員。
告知頁 templates/privacy.html 依個資法第 8 條列出應告知事項；營運者名稱與聯絡方式在後台設定。
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, or_, select, update

from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.sync import PointLedger
from app.models.tenant import Tenant
from app.models.user import PrivacySetting, User
from app.services import support_service
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


def _active_pass_user_ids(tenant_id=None, user_id=None):
    """持有尚有剩餘次數寄杯 (已付款的預收款) 的會員 id"""
    from app.models.member import MemberPass
    query = select(MemberPass.user_id).where(MemberPass.status == 'active', MemberPass.remaining > 0)
    if tenant_id is not None:
        query = query.where(MemberPass.tenant_id == tenant_id)
    if user_id is not None:
        query = query.where(MemberPass.user_id == user_id)
    return set(db.session.execute(query).scalars().all())


def _scan_inactive(tenant_id, months, now=None):
    """
    回傳 (可刪除的會員, 已逾期但仍有儲值金餘額 / 寄杯不可刪除的會員)。
    活動時間：登入 / 登出、消費、點數 / 儲值金 / 集章 / 寄杯異動、同意與手機驗證時間；
    完全沒有活動紀錄 (例如 Excel 匯入、0 點) 時以會員資料的建立 / 最後修改時間 (updated_at) 判斷，
    連這個時間都沒有時無法判斷何時建立，不視為逾期。
    """
    from app.models.member import MemberPass, PassLedger, StampLedger, WalletLedger
    cutoff = (now or datetime.now()) - timedelta(days=30 * months)

    def last(model):
        return select(func.max(model.created_at)).where(model.user_id == User.id).scalar_subquery()
    last_pass = (select(func.max(PassLedger.created_at)).join(MemberPass, MemberPass.id == PassLedger.member_pass_id)
                 .where(MemberPass.user_id == User.id).scalar_subquery())
    # 升級時 (0003) 寫入的期初點數異動 (reason='opening'，created_at=遷移當下) 不代表會員活動，不列入
    last_points = (select(func.max(PointLedger.created_at))
                   .where(PointLedger.user_id == User.id, PointLedger.reason != 'opening').scalar_subquery())
    held_ids = _active_pass_user_ids(tenant_id)
    stale, held = [], []
    for row in db.session.execute(
            select(User, last(Order), last_points, last(WalletLedger), last(StampLedger), last_pass)
            .where(User.tenant_id == tenant_id)).all():
        user = row[0]
        activity = [t for t in (*row[1:], user.last_login_at, user.last_logout_at, user.privacy_consented_at,
                                user.phone_verified_at) if t]
        if not activity:
            if user.updated_at is None:
                continue
            activity = [user.updated_at]
        if max(activity) >= cutoff:
            continue
        if (user.wallet_balance or 0) > 0 or user.id in held_ids:
            held.append(user)   # 已付款的儲值金 / 寄杯不可直接刪除：需先退費或用完
        else:
            stale.append(user)
    return stale, held


def inactive_members(tenant_id, months, now=None):
    """停止使用超過 months 個月、且沒有儲值金餘額與未用完寄杯的會員 (可刪除)"""
    return _scan_inactive(tenant_id, months, now)[0]


def inactive_members_with_balance(tenant_id, months, now=None):
    """停止使用超過 months 個月，但仍有儲值金餘額或未用完寄杯的會員：不會被自動刪除，請店家先聯絡退費"""
    return _scan_inactive(tenant_id, months, now)[1]


def _deidentify_member_records(user):
    """
    會員刪除前的去識別化 (本機)：訂單上的姓名改為「已刪除會員」，已結束配送的外送地址 / 電話清除
    (配送中的訂單保留，外送員仍需要)；進場紀錄 (customer_log「姓名 (手機)」) 改為「已刪除會員」。
    """
    from app.models.enums import OrderStatus
    from app.models.user import CustomerLog
    # 桌邊掃碼訂單的 table_number 是桌號 (不含個資)，保留給廚房 / 看板使用
    db.session.execute(update(Order).where(
        Order.user_id == user.id, Order.dining_table_id.is_(None),
        or_(Order.order_source.is_(None), Order.order_source != 'Table'),
    ).values(table_number='已刪除會員').execution_options(synchronize_session=False))
    db.session.execute(update(Order).where(
        Order.user_id == user.id,
        (Order.delivery_address != '') | (Order.delivery_phone != ''),
        Order.delivery_status.notin_(('pending', 'assigned', 'out')) | (Order.status == OrderStatus.CANCELLED),
    ).values(delivery_address='', delivery_phone='').execution_options(synchronize_session=False))
    if user.phone:
        db.session.execute(update(CustomerLog).where(
            CustomerLog.tenant_id == user.tenant_id, CustomerLog.user_type == '會員',
            CustomerLog.identifier.endswith(f' ({user.phone})', autoescape=True),
        ).values(identifier='已刪除會員').execution_options(synchronize_session=False))


def purge_inactive_members(tenant_id, months, dry_run=True):
    """
    刪除長期未使用的會員 (訂單保留但去識別化)；回傳被刪除 (或將被刪除) 的會員清單。
    仍有儲值金餘額或未用完寄杯的會員不刪除 (見 inactive_members_with_balance)。
    """
    stale = inactive_members(tenant_id, months)
    if dry_run:
        return stale
    from app.services.audit import record_staff_audit
    held_ids = _active_pass_user_ids(tenant_id)
    deleted = []
    for user in stale:
        if (user.wallet_balance or 0) > 0 or user.id in held_ids:   # 掃描後才加值 / 買寄杯：保留
            continue
        withdraw_face(user)
        _deidentify_member_records(user)
        record_staff_audit('PRIVACY_PURGE', target_user_id=user.id, tenant_id=tenant_id, as_system=True,
                           details=f'會員停止使用超過 {months} 個月，依隱私權告知刪除 (會員 #{user.id})')
        support_service.forget_member(user.id)
        db.session.delete(user)
        deleted.append(user)
    db.session.commit()
    return deleted


# ---------------------------------------------------------------------------
# 會員自助：下載個人資料、刪除帳號 (個資法第 3 條：查詢、閱覽、製給複製本、請求刪除)
# ---------------------------------------------------------------------------
def export_member_data(user):
    """會員本人的資料 (JSON 可序列化)：基本資料、訂單、點數 / 儲值金 / 集章異動、優惠券、寄杯、兌換、評分與客服對話"""
    from app.models.coupon import UserCoupon
    from app.models.member import MemberPass, PassLedger, StampLedger, WalletLedger
    from app.models.menu import MenuItem
    from app.models.order import OrderFeedback
    from app.models.support import SupportChat
    from app.models.user import RewardRedemption
    from app.sync import runtime as sync_runtime

    def ts(v):
        return v.isoformat(sep=' ', timespec='seconds') if isinstance(v, datetime) else (v.isoformat() if v else None)

    orders = Order.query.filter_by(user_id=user.id).order_by(Order.id).all()
    passes = MemberPass.query.filter_by(user_id=user.id).order_by(MemberPass.id).all()
    feedback = (OrderFeedback.query.join(Order, Order.id == OrderFeedback.order_id)
                .filter(Order.user_id == user.id).order_by(OrderFeedback.id).all())
    redemptions = RewardRedemption.query.filter_by(user_id=user.id).order_by(RewardRedemption.id).all()
    item_ids = {r.menu_item_id for r in redemptions if r.menu_item_id}
    item_names = dict(db.session.execute(select(MenuItem.id, MenuItem.name).where(MenuItem.id.in_(item_ids))).all()) if item_ids else {}
    data = {
        'exported_at': ts(datetime.now()),
        'profile': {
            'name': user.name, 'phone': user.phone, 'email': user.email or '', 'birthday': ts(user.birthday),
            'marketing_opt_out': bool(user.marketing_opt_out),
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
        'member_passes': [{'name': p.name, 'total_uses': p.total_uses, 'remaining': p.remaining, 'price_paid': p.price_paid,
                           'status': p.status, 'purchased_at': ts(p.created_at),
                           'ledger': [{'at': ts(entry.created_at), 'delta': entry.delta, 'reason': entry.reason}
                                      for entry in PassLedger.query.filter_by(member_pass_id=p.id).order_by(PassLedger.id).all()]}
                          for p in passes],
        'reward_redemptions': [{'at': ts(r.created_at), 'item': item_names.get(r.menu_item_id, ''), 'points_cost': r.points_cost,
                                'status': r.status} for r in redemptions],
        'order_feedback': [{'order_id': f.order_id, 'at': ts(f.created_at), 'rating': f.rating, 'comment': f.comment,
                            'reply': f.reply} for f in feedback],
        'support_chats': [{'at': ts(c.created_at), 'status': c.status,
                           'messages': [{'at': ts(m.created_at), 'sender': m.sender, 'text': m.text} for m in c.messages]}
                          for c in SupportChat.query.filter_by(user_id=user.id).order_by(SupportChat.id).all()],
    }
    if sync_runtime.is_node():
        data['notice'] = '本資料由分店主機匯出：訂單、評分與客服對話只含本分店的紀錄；完整的訂單紀錄請到線上點餐網站登入後下載。'
    return data


def delete_member_account(user, reason='會員自行刪除帳號'):
    """
    會員刪除帳號：人臉資料與照片刪除、訂單保留但去識別化 (帳務需保存)、會員資料刪除 (同步到各分店與線上端)。
    儲值金尚有餘額、或還有未用完的寄杯 (已付款的預收款) 時不可刪除 (需先退費或用完)。由呼叫端 commit 前已處理好登出。
    """
    from app.services.audit import record_staff_audit
    if (user.wallet_balance or 0) > 0:
        raise AppError(f'儲值金尚有 ${user.wallet_balance} 餘額，請先使用完畢或洽櫃檯辦理退費後再刪除帳號', 409)
    if _active_pass_user_ids(user_id=user.id):
        raise AppError('您還有未用完的寄杯，請先使用完畢或洽櫃檯辦理退費後再刪除帳號', 409)
    withdraw_face(user)
    _deidentify_member_records(user)
    record_staff_audit('MEMBER_SELF_DELETE', target_user_id=user.id, tenant_id=user.tenant_id, as_system=True,
                       details=f'{reason} (會員 #{user.id})')
    support_service.forget_member(user.id)   # 客服對話含姓名與對話內容，一併刪除
    db.session.delete(user)
