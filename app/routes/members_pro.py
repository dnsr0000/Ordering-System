"""後台：會員等級、生日、儲值金調整、LINE 推播"""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.member import MemberTier, WalletLedger
from app.models.user import User
from app.services import line_service, settings_service, wallet_service
from app.services.audit import record_staff_audit
from app.utils import get_own, role_required, safe_float, safe_int

members_pro_bp = Blueprint('members_pro', __name__)
TIER_COLORS = ('secondary', 'info', 'warning', 'danger', 'success', 'primary', 'light')


def _user(uid):
    user = get_own(User, uid, '會員')
    return user


@members_pro_bp.route('/admin/members_pro', methods=['GET', 'POST'], endpoint='manage')
@role_required(*MANAGER_ROLES)
def manage():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'add_tier':
            name = (f.get('name') or '').strip()[:30]
            if not name:
                raise AppError('請輸入等級名稱')
            if MemberTier.query.filter_by(name=name).first():
                raise AppError(f'等級「{name}」已存在', 409)
            db.session.add(MemberTier(tenant_id=g.tenant_id, name=name, min_spend=max(0, safe_int(f.get('min_spend'), 0)),
                                      points_multiplier=min(10.0, max(0.0, safe_float(f.get('points_multiplier'), 1.0))),
                                      discount_percent=min(50, max(0, safe_int(f.get('discount_percent'), 0))),
                                      color=f.get('color') if f.get('color') in TIER_COLORS else 'secondary'))
            flash(f'已新增等級「{name}」', 'success')
        elif action == 'edit_tier':
            tier = get_own(MemberTier, f.get('tier_id'), '等級')
            tier.name = (f.get('name') or tier.name).strip()[:30]
            tier.min_spend = max(0, safe_int(f.get('min_spend'), tier.min_spend))
            tier.points_multiplier = min(10.0, max(0.0, safe_float(f.get('points_multiplier'), tier.points_multiplier)))
            tier.discount_percent = min(50, max(0, safe_int(f.get('discount_percent'), tier.discount_percent)))
            tier.color = f.get('color') if f.get('color') in TIER_COLORS else tier.color
            flash('已更新等級', 'success')
        elif action == 'delete_tier':
            tier = get_own(MemberTier, f.get('tier_id'), '等級')
            db.session.delete(tier)
            flash(f'已刪除等級「{tier.name}」', 'success')
        elif action == 'recompute_tiers':
            upgraded = sum(1 for u in User.query.all() if wallet_service.refresh_tier(u))
            flash(f'已重新計算累積消費，{upgraded} 位會員升級', 'success')
        elif action == 'member':
            user = _user(f.get('user_id'))
            raw = (f.get('birthday') or '').strip()
            try:
                user.birthday = date.fromisoformat(raw) if raw else None
            except ValueError:
                raise AppError('生日格式不正確')
            tier_id = safe_int(f.get('tier_id'), 0)
            if tier_id:
                tier = get_own(MemberTier, tier_id, '等級')
                user.tier_id = tier.id
            else:
                user.tier_id = None
            if f.get('unbind_line'):
                user.line_user_id = ''
            record_staff_audit('EDIT_USER', target_user_id=user.id, details=f'會員 {user.name}：生日 / 等級 / LINE 綁定更新')
            flash(f'已更新會員「{user.name}」', 'success')
        elif action == 'wallet_adjust':
            if current_user.role != 'owner':
                raise AppError('只有負責人可以直接調整儲值金', 403)
            user = _user(f.get('user_id'))
            delta = safe_int(f.get('delta'), 0)
            note = (f.get('note') or '').strip()
            if not delta or not note:
                raise AppError('請輸入調整金額與原因')
            if wallet_service.change_balance(g.tenant_id, user.id, delta, 'adjust', note=note,
                                             staff=current_user.username) is None:
                raise AppError('扣除金額超過目前餘額')
            record_staff_audit('WALLET_ADJUST', target_user_id=user.id, details=f'儲值金調整 {delta:+d}：{note}')
            flash(f'已調整「{user.name}」的儲值金 {delta:+d}', 'success')
        elif action == 'birthday_now':
            sent = wallet_service.grant_birthday_gifts(g.tenant_id)
            flash(f'已送出 {sent} 份生日禮 (本月壽星且今年尚未贈送)', 'success')
        elif action == 'broadcast':
            text = (f.get('message') or '').strip()
            if not text:
                raise AppError('請輸入推播內容')
            target = f.get('target') or 'all'
            q = User.query.filter(User.line_user_id != '', User.marketing_opt_out.is_(False))   # 拒絕行銷的會員不推播
            if target.startswith('tier:'):
                q = q.filter(User.tier_id == safe_int(target[5:], 0))
            elif target == 'birthday_month':
                users = [u for u in q.all() if u.birthday and u.birthday.month == date.today().month]
                q = None
            ids = [u.line_user_id for u in (q.all() if q is not None else users)]
            if not ids:
                raise AppError('沒有符合條件且已綁定 LINE 的會員')
            sent = line_service.multicast_text(g.tenant_id, ids, text)
            record_staff_audit('LINE_BROADCAST', details=f'LINE 推播給 {sent} 位會員：{text[:100]}')
            flash(f'已推播給 {sent} 位會員', 'success')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('members_pro.manage', q=request.args.get('q') or None))

    q = (request.args.get('q') or '').strip()
    users_q = User.query
    if q:
        users_q = users_q.filter((User.name.contains(q)) | (User.phone.contains(q)))
    users = users_q.order_by(User.total_spent.desc(), User.id).limit(200).all()
    tiers = wallet_service.tiers()
    stats = {
        'members': User.query.count(),
        'line_bound': User.query.filter(User.line_user_id != '').count(),
        'wallet_total': db.session.query(db.func.coalesce(db.func.sum(User.wallet_balance), 0)).scalar() or 0,
        'birthday_month': sum(1 for u in User.query.filter(User.birthday.isnot(None)).all()
                              if u.birthday.month == date.today().month),
    }
    ledger = WalletLedger.query.order_by(WalletLedger.id.desc()).limit(50).all()
    names = {u.id: u.name for u in User.query.filter(User.id.in_({w.user_id for w in ledger})).all()} if ledger else {}
    return render_template('admin_members_pro.html', users=users, tiers=tiers, stats=stats, q=q, colors=TIER_COLORS,
                           cfg=settings_service.get('member'), line_ready=line_service.messaging_enabled(),
                           ledger=ledger, names=names)
