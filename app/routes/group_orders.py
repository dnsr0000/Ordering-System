"""團體預購 (說明見 app/services/group_order_service.py)：公開的建立 / 參加 / 主揪管理頁，與後台列表"""
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user

from app.models.enums import POS_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.models.group_order import GROUP_STATUS_LABELS, GroupOrder
from app.services import group_order_service as svc
from app.services.audit import record_staff_audit
from app.utils import get_own, role_required, safe_int

group_bp = Blueprint('group_orders', __name__)
MANAGE_SESSION, ENTRY_SESSION = 'group_manage', 'group_entries'
MAX_REMEMBERED = 20


def _remember(key, token, value=True):
    data = dict(session.get(key) or {})
    if value is None:
        data.pop(token, None)
    else:
        data[token] = value
    session[key] = dict(list(data.items())[-MAX_REMEMBERED:])


def _is_manager(group):
    staff = current_user.is_authenticated and getattr(current_user, 'role', '') in POS_ROLES
    return bool((session.get(MANAGE_SESSION) or {}).get(group.token)) or staff


def _group(token):
    svc.ensure_enabled(g.tenant_id)
    return svc.by_token(token)


def _shop():
    return g.tenant.slug


def _my_entries(group):
    keys = (session.get(ENTRY_SESSION) or {}).get(group.token) or []
    return [(k, e) for k in keys for e in [svc.entry_by_key(group, k)] if e is not None]


def _default_pickup(cfg):
    t = datetime.now() + timedelta(minutes=int(cfg.get('min_lead_minutes') or 120) + 30)
    return t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# 主揪建立
# ---------------------------------------------------------------------------
@group_bp.route('/shop/<shop>/group/new', methods=['GET', 'POST'], endpoint='new')
@limiter.limit('10/hour', methods=['POST'])
def new(shop):
    svc.ensure_enabled(g.tenant_id)
    if request.method == 'POST':
        group, key = svc.create(g.tenant_id, request.form)
        db.session.commit()
        _remember(MANAGE_SESSION, group.token)
        resp = make_response(render_template(
            'group_created.html', group=group, shop=_shop(),
            share_url=url_for('group_orders.join', shop=_shop(), token=group.token, _external=True),
            manage_url=url_for('group_orders.manage', shop=_shop(), token=group.token, k=key, _external=True)))
        resp.headers['Cache-Control'] = 'no-store'   # 含管理連結：不快取
        return resp
    cfg = svc.config(g.tenant_id)
    return render_template('group_new.html', cfg=cfg, shop=_shop(), default_pickup=_default_pickup(cfg),
                           now=datetime.now())


# ---------------------------------------------------------------------------
# 參加者
# ---------------------------------------------------------------------------
@group_bp.route('/shop/<shop>/group/<token>', methods=['GET', 'POST'], endpoint='join')
@limiter.limit('60/minute', methods=['POST'])   # 同一間辦公室的同事可能共用一個對外 IP
def join(shop, token):
    group = _group(token)
    if request.args.get('entry'):   # 個人修改連結：記在這個瀏覽器後導回乾淨的網址
        if svc.entry_by_key(group, request.args['entry']) is not None:
            keys = list((session.get(ENTRY_SESSION) or {}).get(group.token) or [])
            if request.args['entry'] not in keys:
                keys.append(request.args['entry'])
            _remember(ENTRY_SESSION, group.token, keys[-10:])
        return redirect(url_for('group_orders.join', shop=_shop(), token=group.token))
    if request.method == 'POST':
        f = request.form
        action = f.get('action', 'add')
        mine = dict(_my_entries(group))
        if action == 'add':
            lines = svc.parse_lines(g.tenant_id, group, f)
            entry, raw = svc.save_entry(group, f.get('name'), lines, f.get('note'))
            db.session.commit()
            keys = list((session.get(ENTRY_SESSION) or {}).get(group.token) or []) + [raw]
            _remember(ENTRY_SESSION, group.token, keys[-10:])
            flash(f'已加入「{entry.name}」的餐點 (可在下方修改)', 'success')
        elif action in ('update', 'delete'):
            entry = mine.get(f.get('entry_key'))
            if entry is None:
                raise AppError('找不到您的點餐 (請用同一個瀏覽器或個人修改連結)', 404)
            if not group.is_open:
                raise AppError('這個團購已截止，無法修改', 409)
            if action == 'update':
                svc.save_entry(group, f.get('name'), svc.parse_lines(g.tenant_id, group, f), f.get('note'), entry=entry)
                flash('已更新您的點餐', 'success')
            else:
                svc.lock_open(group)
                db.session.delete(entry)
                flash('已刪除您的點餐', 'info')
            db.session.commit()
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('group_orders.join', shop=_shop(), token=group.token))
    edit_key = request.args.get('edit')
    mine = _my_entries(group)
    editing = dict(mine).get(edit_key) if edit_key else None
    return render_template('group_join.html', group=group, shop=_shop(), items=svc.menu_items(g.tenant_id),
                           summary=svc.summary(group), mine=mine, editing=editing, edit_key=edit_key,
                           channel='takeout' if group.order_type == '外帶' else 'dine_in',
                           is_manager=_is_manager(group), cfg=svc.config(g.tenant_id))


# ---------------------------------------------------------------------------
# 主揪管理 (管理連結第一次開啟後記在這個瀏覽器；店員登入時也可管理)
# ---------------------------------------------------------------------------
@group_bp.route('/shop/<shop>/group/<token>/manage', methods=['GET', 'POST'], endpoint='manage')
@limiter.limit('30/minute', methods=['POST'])
def manage(shop, token):
    group = _group(token)
    if request.args.get('k'):
        if not svc.check_manage_key(group, request.args['k']):
            abort(404)
        _remember(MANAGE_SESSION, group.token)
        return redirect(url_for('group_orders.manage', shop=_shop(), token=group.token))
    if not _is_manager(group):
        abort(404)
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'remove_entry':
            entry = next((e for e in group.entries if e.id == safe_int(f.get('entry_id'), 0)), None)
            if entry is None:
                raise AppError('查無此筆點餐', 404)
            svc.lock_open(group, ('open', 'failed'))
            db.session.delete(entry)
            db.session.commit()
            flash(f'已移除「{entry.name}」的點餐', 'info')
        elif action == 'toggle_auto':
            group.auto_submit = not group.auto_submit
            db.session.commit()
            flash('截止時會自動送單' if group.auto_submit else '已關閉自動送單 (截止後請按「送單」)', 'success')
        elif action == 'submit':
            group, error = svc.submit(group.id, by=current_user.username if current_user.is_authenticated else '')
            if error:
                raise AppError(f'送單失敗：{error}', 409)
            flash(f'已送單，取餐號 #{group.order.pickup_number:03d}，請於 {group.pickup_at:%m/%d %H:%M} 到店取餐付款',
                  'success')
        elif action == 'cancel':
            svc.cancel(group)
            db.session.commit()
            flash('已取消這個團購', 'info')
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('group_orders.manage', shop=_shop(), token=group.token))
    return render_template('group_manage.html', group=group, shop=_shop(), summary=svc.summary(group),
                           share_url=url_for('group_orders.join', shop=_shop(), token=group.token, _external=True))


@group_bp.route('/shop/<shop>/group/<token>/print', endpoint='print_list')
def print_list(shop, token):
    group = _group(token)
    if not _is_manager(group):
        abort(404)
    return render_template('group_print.html', group=group, summary=svc.summary(group))


# ---------------------------------------------------------------------------
# 後台列表 (店員可代客建立、送單、取消)
# ---------------------------------------------------------------------------
@group_bp.route('/admin/group-orders', methods=['GET', 'POST'], endpoint='admin')
@role_required(*POS_ROLES)
def admin():
    from app.sync import runtime as sync_runtime
    if sync_runtime.is_hub():
        abort(404)
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'create':
            svc.ensure_enabled(g.tenant_id)
            group, _key = svc.create(g.tenant_id, f, staff=current_user.username)
            record_staff_audit('GROUP_ORDER', details=f'代客建立團體預購「{group.title}」{group.organizer_name}')
            db.session.commit()
            flash(f'已建立團購「{group.title}」，請把分享連結傳給客人', 'success')
            return redirect(url_for('group_orders.admin', created=group.token))
        group = get_own(GroupOrder, f.get('group_id'), '團購')
        if action == 'submit':
            group, error = svc.submit(group.id, by=current_user.username)
            if error:
                raise AppError(f'送單失敗：{error}', 409)
            record_staff_audit('GROUP_ORDER', details=f'團體預購「{group.title}」送單 (訂單 #{group.order_id})')
            db.session.commit()
            flash('已送單', 'success')
        elif action == 'cancel':
            svc.cancel(group)
            record_staff_audit('GROUP_ORDER', details=f'取消團體預購「{group.title}」')
            db.session.commit()
            flash('已取消', 'info')
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('group_orders.admin'))
    since = datetime.now() - timedelta(days=7)
    groups = GroupOrder.query.filter(GroupOrder.pickup_at >= since).order_by(GroupOrder.pickup_at).all()
    created = next((x for x in groups if x.token == request.args.get('created')), None)
    cfg = svc.config(g.tenant_id)
    return render_template('admin_group_orders.html', groups=groups, summaries={x.id: svc.summary(x) for x in groups},
                           labels=GROUP_STATUS_LABELS, cfg=cfg, created=created, shop=g.tenant.slug,
                           default_pickup=_default_pickup(cfg))
