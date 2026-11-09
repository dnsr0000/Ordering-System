"""團體預購 (說明見 app/services/group_order_service.py)：公開的建立 / 參加 / 主揪管理頁，與後台列表"""
import json
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user

from app.models.enums import POS_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.models.group_order import GROUP_STATUS_LABELS, GroupOrder, GroupOrderEntry, hash_key
from app.services import branch_service
from app.services import group_order_service as svc
from app.services.audit import record_staff_audit
from app.sync import runtime as sync_runtime
from app.utils import get_own, role_required, safe_int, store_only

group_bp = Blueprint('group_orders', __name__)
MANAGE_SESSION, ENTRY_SESSION = 'group_manage', 'group_entries'
MAX_REMEMBERED = 20
MAX_ENTRY_KEYS = 10   # 每個團購在同一個瀏覽器記住的個人金鑰數 (Cookie session 有大小限制)
NONCE_RE = re.compile(r'^[A-Za-z0-9_-]{16,40}$')


def _remember(key, token, value=True):
    data = dict(session.get(key) or {})
    if value is None:
        data.pop(token, None)
    else:
        data[token] = value
    session[key] = dict(list(data.items())[-MAX_REMEMBERED:])


def _is_staff():
    return current_user.is_authenticated and getattr(current_user, 'role', '') in POS_ROLES


def _is_manager(group):
    return bool((session.get(MANAGE_SESSION) or {}).get(group.token)) or _is_staff()


def _check_shop(shop):
    """已登入員工一律使用所屬店家 (tenant_context)：網址是別家店時明確拒絕，不可默默改用自己的店"""
    if current_user.is_authenticated and str(shop or '').strip().lower() != g.tenant.slug:
        raise AppError(f'您目前以「{g.tenant.name}」員工身分登入，無法開啟其他店家的團購連結；'
                       f'請先登出或改用無痕視窗開啟', 403)


def _group(token, staff_ok=False):
    """staff_ok：店員 (管理頁 / 分裝單) 在功能關閉後仍可處理既有的團購"""
    if not (staff_ok and _is_staff() and not sync_runtime.is_hub()):
        svc.ensure_enabled(g.tenant_id)
    # 本分店的價格 / 上下架：與送單結帳 (process_checkout) 一致
    branch_service.activate(g.tenant_id, branch_service.storefront_branch())
    return svc.by_token(token)


def _shop():
    return g.tenant.slug


def _my_entries(group):
    keys = (session.get(ENTRY_SESSION) or {}).get(group.token) or []
    return [(k, e) for k in keys for e in [svc.entry_by_key(group, k)] if e is not None]


def _remember_entry(group, raw):
    """把個人金鑰記在這個瀏覽器 (已刪除的不再佔位)；超過上限時提示被擠掉的點餐要改用個人修改連結"""
    keys = [k for k in (session.get(ENTRY_SESSION) or {}).get(group.token) or []
            if k != raw and svc.entry_by_key(group, k) is not None] + [raw]
    _remember(ENTRY_SESSION, group.token, keys[-MAX_ENTRY_KEYS:])
    for k in keys[:-MAX_ENTRY_KEYS]:
        e = svc.entry_by_key(group, k)
        if e is not None:
            link = url_for('group_orders.join', shop=_shop(), token=group.token, entry=k, _external=True)
            flash(f'此瀏覽器最多記住 {MAX_ENTRY_KEYS} 筆點餐，「{e.name}」已不在「我的點餐」中，'
                  f'要修改請用個人修改連結：{link}', 'warning')


def _duplicate_entry(group, nonce, name, lines):
    """
    同一張表單重複送出 (連點兩下)：以表單的一次性 nonce 作為個人金鑰，先鎖住團購再查是否已建立。
    回傳 (重複的 entry 或 None, nonce 是否可當新點餐的金鑰)
    """
    if not nonce or group.status != 'open':
        return None, False
    svc.lock_open(group)   # 與同時送出的另一個請求依序執行 (PostgreSQL 等對方提交後才繼續)
    found = GroupOrderEntry.query.filter_by(group_order_id=group.id, entry_key_hash=hash_key(nonce)) \
        .execution_options(populate_existing=True).first()
    if found is None:
        return None, True
    same = found.name == str(name or '').strip()[:30] and found.items == json.dumps(lines, ensure_ascii=False)
    return (found if same else None), False


def _default_pickup(cfg):
    """預填的取餐時間：最短提前時間再多 30 分鐘後往上取整到 :00 / :30 (保留至少 30 分鐘填表時間)"""
    t = datetime.now() + timedelta(minutes=int(cfg.get('min_lead_minutes') or 120) + 30)
    slot = t.replace(minute=0, second=0, microsecond=0)
    while slot < t:
        slot += timedelta(minutes=30)
    return slot


# ---------------------------------------------------------------------------
# 主揪建立
# ---------------------------------------------------------------------------
@group_bp.route('/shop/<shop>/group/new', methods=['GET', 'POST'], endpoint='new')
@limiter.limit('10/hour', methods=['POST'])
def new(shop):
    _check_shop(shop)
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
    _check_shop(shop)
    group = _group(token)
    if request.args.get('entry'):   # 個人修改連結：記在這個瀏覽器後導回乾淨的網址
        if svc.entry_by_key(group, request.args['entry']) is not None:
            _remember_entry(group, request.args['entry'])
        return redirect(url_for('group_orders.join', shop=_shop(), token=group.token))
    if request.method == 'POST':
        f = request.form
        action = f.get('action', 'add')
        mine = dict(_my_entries(group))
        if action == 'add':
            lines = svc.parse_lines(g.tenant_id, group, f)
            nonce = f.get('entry_nonce') if NONCE_RE.match(str(f.get('entry_nonce') or '')) else ''
            entry, use_nonce = _duplicate_entry(group, nonce, f.get('name'), lines)
            if entry is not None:   # 重複送出：不再建立，記住同一把金鑰
                raw = nonce
            else:
                entry, raw = svc.save_entry(group, f.get('name'), lines, f.get('note'))
                if use_nonce:   # 以表單 nonce 為金鑰：同時送出的兩個請求寫回的 Cookie 都是同一把
                    entry.entry_key_hash, raw = hash_key(nonce), nonce
            db.session.commit()
            _remember_entry(group, raw)
            flash(f'已加入「{entry.name}」的餐點 (可在下方修改)', 'success')
        elif action in ('update', 'delete'):
            entry = mine.get(f.get('entry_key'))
            if entry is None:
                raise AppError('找不到您的點餐 (請用同一個瀏覽器或個人修改連結)', 404)
            if not group.is_open:
                raise AppError('這個團購已截止，無法修改', 409)
            entry_id = entry.id
            svc.lock_open(group)   # 先鎖住團購再確認這筆仍存在 (主揪可能剛移除)
            if db.session.execute(db.select(GroupOrderEntry.id).where(GroupOrderEntry.id == entry_id)).first() is None:
                raise AppError('找不到您的點餐，可能已被主揪移除', 404)
            if action == 'update':
                svc.save_entry(group, f.get('name'), svc.parse_lines(g.tenant_id, group, f), f.get('note'), entry=entry)
                flash('已更新您的點餐', 'success')
            else:
                db.session.delete(entry)
                flash('已刪除您的點餐', 'info')
            db.session.commit()
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('group_orders.join', shop=_shop(), token=group.token))
    edit_key = request.args.get('edit')
    mine = _my_entries(group)
    editing = dict(mine).get(edit_key) if edit_key else None
    return render_template('group_join.html', group=group, shop=_shop(), items=svc.menu_items(g.tenant_id, now=group.pickup_at),
                           summary=svc.summary(group), mine=mine, editing=editing, edit_key=edit_key,
                           entry_nonce=secrets.token_urlsafe(18),
                           channel='takeout' if group.order_type == '外帶' else 'dine_in',
                           is_manager=_is_manager(group), cfg=svc.config(g.tenant_id))


# ---------------------------------------------------------------------------
# 主揪管理 (管理連結第一次開啟後記在這個瀏覽器；店員登入時也可管理)
# ---------------------------------------------------------------------------
@group_bp.route('/shop/<shop>/group/<token>/manage', methods=['GET', 'POST'], endpoint='manage')
@limiter.limit('30/minute', methods=['POST'])
def manage(shop, token):
    _check_shop(shop)
    group = _group(token, staff_ok=True)
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
    _check_shop(shop)
    group = _group(token, staff_ok=True)
    if not _is_manager(group):
        abort(404)
    return render_template('group_print.html', group=group, summary=svc.summary(group))


# ---------------------------------------------------------------------------
# 後台列表 (店員可代客建立、送單、取消)
# ---------------------------------------------------------------------------
@group_bp.route('/admin/group-orders', methods=['GET', 'POST'], endpoint='admin')
@role_required(*POS_ROLES)
@store_only
def admin():
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
    branch_service.activate(g.tenant_id, branch_service.storefront_branch())   # 預估金額依本分店價格
    since = datetime.now() - timedelta(days=7)
    groups = GroupOrder.query.filter(GroupOrder.pickup_at >= since).order_by(GroupOrder.pickup_at).all()
    created = next((x for x in groups if x.token == request.args.get('created')), None)
    cfg = svc.config(g.tenant_id)
    return render_template('admin_group_orders.html', groups=groups, summaries={x.id: svc.summary(x) for x in groups},
                           labels=GROUP_STATUS_LABELS, cfg=cfg, created=created, shop=g.tenant.slug,
                           default_pickup=_default_pickup(cfg))
