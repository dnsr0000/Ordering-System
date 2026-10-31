"""後台：促銷活動 (買 X 送 Y、第 N 件折扣、滿額折、滿額贈)；前台購物車預覽 API"""
from datetime import date

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.models.menu import MenuItem
from app.models.promotion import PROMOTION_KINDS, Promotion
from app.services import promotion_service
from app.services.audit import record_staff_audit
from app.services.customer_session import current_member
from app.services.order_service import _parse_lines
from app.services.settings_service import TIME_RE
from app.sync import runtime as sync_runtime
from app.utils import find_own, get_json_dict, get_own, role_required, safe_int

promotions_bp = Blueprint('promotions', __name__)
CHANNELS = (('kiosk', '點餐機'), ('pos', 'POS'), ('table', '桌邊掃碼'), ('online', '線上點餐'))


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise AppError(f'日期格式不正確：{value}')


def _apply(p, f):
    name = (f.get('name') or '').strip()[:60]
    if not name:
        raise AppError('請輸入活動名稱')
    if f.get('kind') not in PROMOTION_KINDS:
        raise AppError('活動類型不正確')
    p.name, p.kind = name, f.get('kind')
    p.target_type = f.get('target_type') if f.get('target_type') in ('all', 'category', 'items') else 'all'
    if p.target_type == 'category':
        p.target_value = ','.join(c.strip() for c in f.getlist('target_categories') if c.strip())
    elif p.target_type == 'items':
        p.target_value = ','.join(u for u in f.getlist('target_items') if u)
    else:
        p.target_value = ''
    if p.target_type != 'all' and not p.target_value:
        raise AppError('請選擇適用的分類或品項')
    p.buy_qty = max(1, safe_int(f.get('buy_qty'), 1))
    p.get_qty = max(1, safe_int(f.get('get_qty'), 1))
    p.nth = max(2, safe_int(f.get('nth'), 2))
    p.percent = min(100, max(0, safe_int(f.get('percent'), 50)))
    p.amount = max(0, safe_int(f.get('amount'), 0))
    p.threshold = max(0, safe_int(f.get('threshold'), 0))
    p.gift_item_uuid = (f.get('gift_item_uuid') or '').strip()[:36]
    if p.kind in ('threshold_discount', 'threshold_gift') and p.threshold <= 0:
        raise AppError('滿額活動請設定門檻金額')
    if p.kind == 'threshold_gift' and not p.gift_item_uuid:
        raise AppError('請選擇贈品')
    if p.kind == 'threshold_discount' and p.amount <= 0 and p.percent >= 100:
        raise AppError('請設定折抵金額或折扣 (應付百分比小於 100)')
    p.start_date, p.end_date = _date(f.get('start_date')), _date(f.get('end_date'))
    days = ''.join(sorted({d for d in f.getlist('days') if d in '1234567'}))
    p.days = days or '1234567'
    for field in ('start_time', 'end_time'):
        v = (f.get(field) or '').strip()
        if v and not TIME_RE.match(v):
            raise AppError('時段格式應為 HH:MM')
        setattr(p, field, v)
    p.channels = ','.join(c for c in f.getlist('channels') if c in dict(CHANNELS))
    p.member_only = bool(f.get('member_only'))
    p.stack_with_coupon = bool(f.get('stack_with_coupon'))
    p.priority = safe_int(f.get('priority'), 0)
    p.is_active = bool(f.get('is_active'))
    return p


@promotions_bp.route('/admin/promotions', methods=['GET', 'POST'], endpoint='manage')
@role_required(*MANAGER_ROLES)
def manage():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save':
            pid = safe_int(request.form.get('promotion_id'), 0)
            if pid:
                p = get_own(Promotion, pid, '活動')
            else:
                p = Promotion(tenant_id=g.tenant_id)
                db.session.add(p)
            _apply(p, request.form)
            record_staff_audit('PROMOTION_CHANGE', details=f"{'編輯' if pid else '新增'}促銷活動：{p.name} ({p.kind_label})")
            flash(f'已儲存活動「{p.name}」', 'success')
        elif action in ('toggle', 'delete'):
            p = get_own(Promotion, request.form.get('promotion_id'), '活動')
            if action == 'toggle':
                p.is_active = not p.is_active
            else:
                db.session.delete(p)
            record_staff_audit('PROMOTION_CHANGE', details=f'{action} 促銷活動：{p.name}')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('promotions.manage'))
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    edit = find_own(Promotion, request.args.get('edit')) if request.args.get('edit') else None
    return render_template('admin_promotions.html', promotions=Promotion.query.order_by(Promotion.priority.desc(), Promotion.id).all(),
                           items=items, item_by_uuid={i.uuid: i for i in items},
                           categories=sorted({(i.category or '').strip() for i in items if i.category}),
                           kinds=PROMOTION_KINDS, channels=CHANNELS, edit=edit)


@promotions_bp.route('/api/promotions/preview', methods=['POST'])
@limiter.limit('120/minute')
def preview():
    """購物車預覽：目前購物車可套用的促銷 (結帳時以後端重新計算為準)"""
    data = get_json_dict()
    channel = str(data.get('channel') or 'kiosk')
    if sync_runtime.is_hub():
        channel = 'online'
    price_channel = {'online': 'online'}.get(channel, 'takeout' if data.get('order_type') == '外帶' else 'dine_in')
    from app.services import branch_service
    branch_service.activate_for_request(g.tenant_id)   # 分店價格 / 下架與結帳一致
    try:
        lines = _parse_lines(data.get('items'), g.tenant_id, price_channel, allow_override=False)
    except AppError:
        return jsonify({'success': True, 'discount': 0, 'names': [], 'details': [], 'gifts': [], 'blocks_coupon': False})
    has_member = current_member() is not None or bool(safe_int(data.get('user_id'), 0))
    result = promotion_service.evaluate(lines, channel, has_member=has_member)
    return jsonify(dict(result.to_dict(), success=True))
