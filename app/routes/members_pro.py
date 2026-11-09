"""後台：會員頁 (/admin/members)：名冊 (分頁、搜尋、排序；編輯 / 刪除 / 票券的 POST 在 admin.py)、
會員等級、生日、儲值金調整、LINE 推播"""
from datetime import date

import os

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db
from app.models.member import MemberTier, WalletLedger
from app.models.user import User
from app.services import coupon_service, line_service, settings_service, wallet_service
from app.services.audit import record_staff_audit
from app.services.approval_service import permission_required
from app.services.upload_paths import upload_file_path
from app.sync import runtime as sync_runtime
from app.utils import get_own, paginate, parse_birthday, safe_float, safe_int

members_pro_bp = Blueprint('members_pro', __name__)
TIER_COLORS = ('secondary', 'info', 'warning', 'danger', 'success', 'primary', 'light')
MAX_TIER_SPEND = 10_000_000      # 等級累積消費門檻上限 (元)；超過資料庫 INTEGER 範圍會 500
MAX_WALLET_ADJUST = 1_000_000    # 單次直接調整儲值金的上限 (元)


def _user(uid):
    user = get_own(User, uid, '會員')
    return user


def _capped(value, limit, label):
    """數字欄位超出合理範圍 (例如 99999999999、1e30) 時回 400，不讓資料庫拋錯變成 500"""
    if abs(value) > limit:
        raise AppError(f'{label}不可超過 {limit:,}')
    return value


def _tier_name(f, tier_id=None):
    """等級名稱：不可空白、不可與其他等級同名 (member_tier 有 (tenant_id, name) 唯一約束)"""
    name = (f.get('name') or '').strip()[:30]
    if not name:
        raise AppError('請輸入等級名稱')
    q = MemberTier.query.filter(MemberTier.name == name)
    if tier_id is not None:
        q = q.filter(MemberTier.id != tier_id)
    if q.first():
        raise AppError(f'等級「{name}」已存在', 409)
    return name


@members_pro_bp.route('/admin/members', methods=['GET', 'POST'], endpoint='manage')
@permission_required('members')
def manage():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'add_tier':
            name = _tier_name(f)
            min_spend = max(0, _capped(safe_int(f.get('min_spend'), 0), MAX_TIER_SPEND, '累積消費門檻'))
            db.session.add(MemberTier(tenant_id=g.tenant_id, name=name, min_spend=min_spend,
                                      points_multiplier=min(10.0, max(0.0, safe_float(f.get('points_multiplier'), 1.0))),
                                      discount_percent=min(50, max(0, safe_int(f.get('discount_percent'), 0))),
                                      color=f.get('color') if f.get('color') in TIER_COLORS else 'secondary'))
            flash(f'已新增等級「{name}」', 'success')
        elif action == 'edit_tier':
            tier = get_own(MemberTier, f.get('tier_id'), '等級')
            tier.name = _tier_name(f, tier.id)
            tier.min_spend = max(0, _capped(safe_int(f.get('min_spend'), tier.min_spend), MAX_TIER_SPEND, '累積消費門檻'))
            tier.points_multiplier = min(10.0, max(0.0, safe_float(f.get('points_multiplier'), tier.points_multiplier)))
            tier.discount_percent = min(50, max(0, safe_int(f.get('discount_percent'), tier.discount_percent)))
            tier.color = f.get('color') if f.get('color') in TIER_COLORS else tier.color
            flash('已更新等級', 'success')
        elif action == 'delete_tier':
            tier = get_own(MemberTier, f.get('tier_id'), '等級')
            members = User.query.filter(User.tier_id == tier.id).count()
            db.session.delete(tier)
            record_staff_audit('MEMBER_TIER_DELETE', details=f'刪除會員等級「{tier.name}」(原有 {members} 位會員，已改為無等級)')
            flash(f'已刪除等級「{tier.name}」', 'success')
        elif action == 'recompute_tiers':
            # 未啟用等級或尚未建立等級時 refresh_tier 不會重算累積消費：這裡仍要重算 (部分退款後的累積消費要扣回)
            tiers_on = settings_service.get('member').get('tiers_enabled', True) and bool(wallet_service.tiers())
            upgraded = 0
            for u in User.query.all():
                if tiers_on:
                    upgraded += bool(wallet_service.refresh_tier(u))
                else:
                    wallet_service.recompute_spent(u)
            flash(f'已重新計算累積消費，{upgraded} 位會員升級' if tiers_on
                  else '已重新計算累積消費 (未啟用會員等級或尚未建立等級，等級未調整)', 'success')
        elif action == 'member':
            user = _user(f.get('user_id'))
            user.birthday = parse_birthday(f)
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
            delta = _capped(safe_int(f.get('delta'), 0), MAX_WALLET_ADJUST, '儲值金調整金額')
            note = (f.get('note') or '').strip()
            if not delta or not note:
                raise AppError('請輸入調整金額與原因')
            if wallet_service.change_balance(g.tenant_id, user.id, delta, 'adjust', note=note,
                                             staff=current_user.username) is None:
                raise AppError('扣除金額超過目前餘額')
            record_staff_audit('WALLET_ADJUST', target_user_id=user.id, details=f'儲值金調整 {delta:+d}：{note}')
            flash(f'已調整「{user.name}」的儲值金 {delta:+d}', 'success')
        elif action == 'birthday_now':
            if sync_runtime.is_node():
                # 與每日排程一致：分店不送 (各分店 / 線上端各送一次會重複發點數與優惠券)
                raise AppError('生日禮由總店 / 線上端送出，分店主機不執行', 403)
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
            partial = getattr(sent, 'error', '')
            record_staff_audit('LINE_BROADCAST', details=f'LINE 推播給 {sent} 位會員{"(部分失敗)" if partial else ""}：{text[:100]}')
            flash(partial or f'已推播給 {sent} 位會員', 'warning' if partial else 'success')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('members_pro.manage', q=request.args.get('q') or None, sort=request.args.get('sort') or None,
                                page=request.args.get('page') or None))

    # 名冊：分頁 + 搜尋 + 排序 (會員數上千時不一次載入全部；持券於開啟票券視窗時才載入)
    q = (request.args.get('q') or '').strip()[:50]
    sort = 'spent' if request.args.get('sort') == 'spent' else ''
    users_q = User.query
    if q:
        users_q = users_q.filter((User.name.contains(q)) | (User.phone.contains(q)))
    order = (User.total_spent.desc(), User.id) if sort == 'spent' else (User.id.desc(),)
    users, page, pages, total = paginate(users_q.order_by(*order))
    member_dir = current_app.config['UPLOAD_FOLDER_MEMBER']
    tiers = wallet_service.tiers()
    # 各等級的會員人數 (刪除等級的確認視窗顯示會影響多少位會員)
    tier_counts = dict(User.query.with_entities(User.tier_id, db.func.count(User.id))
                       .filter(User.tier_id.isnot(None)).group_by(User.tier_id).all())
    stats = {
        'members': User.query.count(),
        'line_bound': User.query.filter(User.line_user_id != '').count(),
        'wallet_total': db.session.query(db.func.coalesce(db.func.sum(User.wallet_balance), 0)).scalar() or 0,
        'birthday_month': sum(1 for u in User.query.filter(User.birthday.isnot(None)).all()
                              if u.birthday.month == date.today().month),
    }
    ledger = WalletLedger.query.order_by(WalletLedger.id.desc()).limit(50).all()
    names = {u.id: u.name for u in User.query.filter(User.id.in_({w.user_id for w in ledger})).all()} if ledger else {}
    return render_template('admin_members.html', users=users, tiers=tiers, stats=stats, q=q, sort=sort, page=page, pages=pages,
                           total=total, colors=TIER_COLORS, tier_counts=tier_counts, is_node=sync_runtime.is_node(),
                           max_tier_spend=MAX_TIER_SPEND, max_wallet_adjust=MAX_WALLET_ADJUST,
                           # 只顯示實際存在的會員照片 (檔案遺失時改顯示預設圖，避免瀏覽器出現 404)
                           member_photo_ids={u.id for u in users if os.path.isfile(upload_file_path(member_dir, u.photo_path) or '')},
                           coupon_counts={u.id: coupon_service.unused_count(u) for u in users},
                           coupon_history_days=coupon_service.HISTORY_DAYS,
                           cfg=settings_service.get('member'), line_ready=line_service.messaging_enabled(),
                           ledger=ledger, names=names)
