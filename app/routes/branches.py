"""後台：分店價格 / 分店上下架 (總部設定)、分店叫貨 / 中央廚房 / 分店間調撥 (說明見 app/services/branch_service.py)"""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.branch import HQ_SITE, TRANSFER_STATUS_LABELS
from app.models.inventory import Ingredient
from app.models.menu import MenuItem
from app.services import branch_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.sync import runtime as sync_runtime
from app.utils import role_required

branches_bp = Blueprint('branches', __name__)


# ---------------------------------------------------------------------------
# 分店價格 / 上下架
# ---------------------------------------------------------------------------
@branches_bp.route('/admin/branch-menu', methods=['GET', 'POST'], endpoint='menu')
@role_required(*MANAGER_ROLES)
def branch_menu():
    role = sync_runtime.role()
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    if role == sync_runtime.HUB:
        from app.models.sync import SyncNode
        nodes = SyncNode.query.order_by(SyncNode.id).all()
        selected = request.values.get('node') or (nodes[0].uuid if nodes else '')
        node = next((n for n in nodes if n.uuid == selected), None)
        if request.method == 'POST':
            if node is None:
                raise AppError('請先選擇分店', redirect_to=url_for('branches.menu'))
            rows = [{'menu_item_id': i.id, 'price': (request.form.get(f'price_{i.id}') or '').strip(),
                     'hidden': request.form.get(f'hidden_{i.id}') == '1'} for i in items]
            changed, notes = branch_service.save_overrides(g.tenant_id, node.uuid, rows, current_user.username)
            if changed:
                record_staff_audit('BRANCH_MENU', details=f'{node.name} 分店價格 / 上下架：' + '、'.join(notes)[:400])
            db.session.commit()
            flash(f'已更新【{node.name}】{changed} 項分店價格 / 上下架 (下次同步時送到分店)' if changed
                  else '沒有變更', 'success' if changed else 'info')
            return redirect(url_for('branches.menu', node=node.uuid))
        overrides = branch_service.overrides_for(g.tenant_id, node.uuid) if node else {}
        return render_template('admin_branch_menu.html', mode='hub', nodes=nodes, node=node, items=items,
                               overrides=overrides)
    if request.method == 'POST':
        raise AppError('分店價格 / 上下架由總部 (線上端) 設定', 403)
    overrides = branch_service.overrides_for(g.tenant_id, sync_runtime.local_node_uuid()) \
        if role == sync_runtime.NODE and sync_runtime.local_node_uuid() else {}
    return render_template('admin_branch_menu.html', mode=role, nodes=[], node=None, items=items, overrides=overrides)


# ---------------------------------------------------------------------------
# 叫貨 / 調撥
# ---------------------------------------------------------------------------
def _lines_from_form(form):
    names, qtys = form.getlist('line_name'), form.getlist('line_qty')
    return [{'name': n, 'qty': q} for n, q in zip(names, qtys) if (n or '').strip()]


@branches_bp.route('/admin/transfers', methods=['GET', 'POST'], endpoint='transfers')
@permission_required('inventory')
def transfers():
    me = branch_service.local_site()
    if request.method == 'POST':
        if not me:
            raise AppError('叫貨 / 調撥需要啟用多分店同步', redirect_to=url_for('branches.transfers'))
        f = request.form
        back = url_for('branches.transfers')
        try:
            if f.get('action') == 'create_request':
                tr = branch_service.create_request(g.tenant_id, f.get('site'), _lines_from_form(f), f.get('note'),
                                                   current_user.username)
                msg = f'✅ 已建立叫貨單 {tr.number} (向{tr.from_name}叫貨)'
            elif f.get('action') == 'create_transfer':
                tr = branch_service.create_transfer(g.tenant_id, f.get('site'), _lines_from_form(f), f.get('note'),
                                                    current_user.username)
                msg = f'✅ 已調撥出貨 {tr.number} 給{tr.to_name} (已扣本店庫存)'
            else:
                raise AppError('不支援的操作')
        except AppError as exc:
            exc.redirect_to = exc.redirect_to or back
            raise
        record_staff_audit('STOCK_TRANSFER', details=f'{msg[2:]}：' + '、'.join(
            f"{ln['name']} {ln['qty']:g}{ln['unit']}" for ln in tr.lines)[:300])
        db.session.commit()
        flash(msg, 'success')
        return redirect(url_for('branches.transfer_detail', transfer_id=tr.id))
    rows = branch_service.transfers(g.tenant_id)
    sites = [s for s in branch_service.sites(g.tenant_id) if s['uuid'] != me and s.get('active', True)] if me else []
    ingredients = Ingredient.query.filter_by(is_active=True).order_by(Ingredient.name).all()
    todo_ship = [t for t in rows if t.from_site == me and t.status == 'requested']
    todo_receive = [t for t in rows if t.to_site == me and t.status == 'shipped']
    return render_template('admin_transfers.html', me=me, rows=rows, sites=sites, ingredients=ingredients,
                           todo_ship=todo_ship, todo_receive=todo_receive, labels=TRANSFER_STATUS_LABELS,
                           hq=HQ_SITE, role=sync_runtime.role())


@branches_bp.route('/admin/transfers/<int:transfer_id>', methods=['GET', 'POST'], endpoint='transfer_detail')
@permission_required('inventory')
def transfer_detail(transfer_id):
    tr = branch_service.get_transfer(g.tenant_id, transfer_id)
    back = url_for('branches.transfer_detail', transfer_id=tr.id)
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        qty = {ln['name']: f.get(f'qty_{i}') for i, ln in enumerate(tr.lines)}
        try:
            if action == 'ship':
                branch_service.ship(g.tenant_id, tr.id, qty, current_user.username)
                msg = f'✅ {tr.number} 已出貨給{tr.to_name} (已扣本店庫存)'
            elif action == 'reject':
                branch_service.reject(g.tenant_id, tr.id, f.get('reason'), current_user.username)
                msg = f'✅ 已拒絕 {tr.number}'
            elif action == 'cancel':
                branch_service.cancel(g.tenant_id, tr.id, f.get('reason'), current_user.username)
                msg = f'✅ 已取消叫貨單 {tr.number}'
            elif action == 'receive':
                branch_service.receive(g.tenant_id, tr.id, qty, current_user.username, f.get('reason'))
                msg = f'✅ {tr.number} 已收貨入庫'
            else:
                raise AppError('不支援的操作')
        except AppError as exc:
            exc.redirect_to = exc.redirect_to or back
            raise
        record_staff_audit('STOCK_TRANSFER', details=msg[2:])
        db.session.commit()
        flash(msg, 'success')
        return redirect(back)
    return render_template('admin_transfer_detail.html', tr=tr, me=branch_service.local_site(),
                           labels=TRANSFER_STATUS_LABELS)
