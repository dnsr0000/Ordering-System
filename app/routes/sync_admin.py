"""
同步相關的後台頁面 (說明見 SYNC.txt)：
- 線上端 (hub)：/admin/nodes 分店管理 (負責人限定)：新增分店、權杖、開放線上點餐、停用/啟用
- 分店端 (node)：/admin/sync 同步狀態 (負責人/店長)：狀態、衝突清單、立即同步
單機 (standalone) 模式下這些頁面一律 404。
"""
from datetime import datetime
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.sync import NodeStock, SyncNode
from app.services.audit import record_staff_audit
from app.sync import runtime
from app.sync.node import NodeSync, record_error, record_ok, make_transport, sync_status
from app.sync.nodes import create_node, rotate_token
from app.utils import role_required, safe_int

sync_admin_bp = Blueprint('sync_admin', __name__)

_TOKEN_ONCE_KEY = 'sync_node_token_once'


@sync_admin_bp.app_context_processor
def _inject_sync_role():
    """所有樣板都可用 sync_role 判斷目前角色 (standalone / hub / node)"""
    return {'sync_role': runtime.role()}


def only_role(role):
    """只在指定的同步角色提供此頁面，其他角色 404 (放在 role_required 之後：未登入者一律先導向登入)"""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if runtime.role() != role:
                abort(404)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def node_name_map():
    """線上端：分店 uuid → 名稱 (訂單顯示取餐分店用)；其他角色回傳空字典"""
    if not runtime.is_hub():
        return {}
    return dict(db.session.query(SyncNode.uuid, SyncNode.name).filter(SyncNode.tenant_id == g.tenant_id).all())


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else ''


# ==============================================================================
# 線上端：分店管理
# ==============================================================================
@sync_admin_bp.route('/admin/nodes', methods=['GET'], endpoint='nodes')
@role_required('owner')
@only_role(runtime.HUB)
def nodes():
    rows = SyncNode.query.filter_by(tenant_id=g.tenant_id).order_by(SyncNode.id).all()
    stock_info = {}
    if rows:
        stock_info = {node_id: (reported, count) for node_id, reported, count in db.session.query(
            NodeStock.node_id, func.max(NodeStock.reported_at), func.count(NodeStock.menu_item_id))
            .filter(NodeStock.node_id.in_([n.id for n in rows])).group_by(NodeStock.node_id).all()}
    return render_template(
        'admin_nodes.html',
        tenant=g.tenant,
        nodes=[{
            'id': n.id, 'uuid': n.uuid, 'name': n.name, 'is_active': bool(n.is_active),
            'accepts_online_orders': bool(n.accepts_online_orders), 'created_at': _fmt(n.created_at),
            'last_seen_at': _fmt(n.last_seen_at), 'last_ip': n.last_ip or '',
            'stock_reported_at': _fmt(stock_info.get(n.id, (None, 0))[0]),
            'stock_items': stock_info.get(n.id, (None, 0))[1],
        } for n in rows],
        new_token=session.pop(_TOKEN_ONCE_KEY, None),
        hub_url=request.host_url.rstrip('/'),
    )


def _show_token_once(node, token):
    """權杖只顯示一次：存入 session，下一次載入分店管理頁時取出並刪除 (資料庫只存雜湊)"""
    session[_TOKEN_ONCE_KEY] = {'name': node.name, 'uuid': node.uuid, 'token': token}


@sync_admin_bp.route('/admin/nodes', methods=['POST'], endpoint='nodes_action')
@role_required('owner')
@only_role(runtime.HUB)
def nodes_action():
    back = url_for('sync_admin.nodes')
    action = request.form.get('action', '')
    if action == 'create':
        name = (request.form.get('name') or '').strip()
        if not name:
            raise AppError('❌ 請輸入分店名稱！', redirect_to=back)
        node, token = create_node(g.tenant_id, name, accepts_online_orders=request.form.get('accepts_online_orders') == '1')
        record_staff_audit('SYNC_NODE_CREATE', details=f'新增分店：{node.name} ({node.uuid})')
        from app.services import branch_service
        branch_service.refresh_sites(g.tenant_id)
        db.session.commit()
        _show_token_once(node, token)
        flash(f'已新增分店【{node.name}】，請立即複製下方的權杖 (只會顯示這一次)。', 'success')
        return redirect(back)

    node = SyncNode.query.filter_by(id=safe_int(request.form.get('node_id')), tenant_id=g.tenant_id).first_or_404()
    if action == 'rotate_token':
        token = rotate_token(node)
        record_staff_audit('SYNC_NODE_ROTATE_TOKEN', details=f'重新產生分店權杖：{node.name} ({node.uuid})')
        db.session.commit()
        _show_token_once(node, token)
        flash(f'已重新產生【{node.name}】的權杖，舊權杖已失效；請更新該分店的 SYNC_NODE_TOKEN。', 'success')
        return redirect(back)
    if action == 'toggle_online':
        node.accepts_online_orders = not node.accepts_online_orders
        record_staff_audit('SYNC_NODE_TOGGLE_ONLINE',
                           details=f"{'開放' if node.accepts_online_orders else '暫停'}線上點餐：{node.name}")
    elif action == 'toggle_active':
        node.is_active = not node.is_active
        record_staff_audit('SYNC_NODE_TOGGLE_ACTIVE', details=f"{'啟用' if node.is_active else '停用'}分店：{node.name}")
    elif action == 'rename':
        name = (request.form.get('name') or '').strip()[:100]
        if not name:
            raise AppError('❌ 分店名稱不可空白！', redirect_to=back)
        record_staff_audit('SYNC_NODE_RENAME', details=f'分店改名：{node.name} → {name}')
        node.name = name
    else:
        raise AppError('不支援的操作', redirect_to=back)
    from app.services import branch_service
    branch_service.refresh_sites(g.tenant_id)   # 分店名單 (叫貨 / 調撥選擇對象) 下發給各分店
    db.session.commit()
    return redirect(back)


# ==============================================================================
# 分店端：同步狀態
# ==============================================================================
@sync_admin_bp.route('/admin/sync', methods=['GET'], endpoint='sync_status')
@role_required(*MANAGER_ROLES)
@only_role(runtime.NODE)
def sync_status_page():
    status = sync_status()
    return render_template(
        'admin_sync.html',
        tenant=g.tenant,
        status=status,
        configured=bool(current_app.config.get('SYNC_HUB_URL') and current_app.config.get('SYNC_NODE_TOKEN')),
        background=current_app.extensions.get('sync_worker') is not None,
        interval=current_app.config.get('SYNC_INTERVAL'),
        now=_fmt(datetime.now()),
    )


@sync_admin_bp.route('/admin/sync/run', methods=['POST'], endpoint='sync_run')
@role_required(*MANAGER_ROLES)
@only_role(runtime.NODE)
def sync_run():
    back = url_for('sync_admin.sync_status')
    worker = current_app.extensions.get('sync_worker')
    if worker is not None:
        worker.local_change.set()
        flash('已通知背景同步立即執行，請稍候幾秒後重新整理本頁查看結果。', 'success')
        return redirect(back)
    try:
        pushed, pulled = NodeSync(make_transport(current_app)).run_once()
    except Exception as exc:  # noqa: BLE001  (斷網、權杖錯誤等：記錄後顯示給店員)
        try:
            record_error(exc)
        except Exception:  # noqa: BLE001
            db.session.rollback()
        flash(f'同步失敗：{exc}', 'danger')
        return redirect(back)
    record_ok()
    flash(f'同步完成：上傳 {pushed} 筆、下載 {pulled} 筆變更。', 'success')
    return redirect(back)
