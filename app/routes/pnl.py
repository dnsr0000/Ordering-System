"""後台：營業費用登錄、損益表、原料成本 / 售價歷史 (說明見 app/services/pnl_service.py)"""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db
from app.models.expense import EXPENSE_CATEGORIES, Expense
from app.services import pnl_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.services.history_service import FIELD_LABELS
from app.sync import runtime as sync_runtime
from app.utils import role_required, safe_int

pnl_bp = Blueprint('pnl', __name__)


def _month():
    month = request.values.get('month') or f'{date.today():%Y-%m}'
    start, end = pnl_service.month_range(month)
    return f'{start:%Y-%m}', start, end


@pnl_bp.route('/admin/expenses', methods=['GET', 'POST'], endpoint='expenses')
@role_required('owner', 'manager')
def expenses():
    month, start, end = _month()
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'add':
            e = pnl_service.save_expense(Expense(tenant_id=g.tenant_id), f, current_user.username)
            db.session.add(e)
            record_staff_audit('EXPENSE', details=f'新增費用 {e.spent_on} {e.category_label} {e.name} ${e.amount}')
            flash(f'✅ 已新增費用「{e.name}」${e.amount:,}', 'success')
        elif action in ('edit', 'delete'):
            e = db.session.get(Expense, safe_int(f.get('expense_id'), 0))
            if e is None or e.tenant_id != g.tenant_id or e.is_deleted:
                raise AppError('查無此費用', 404)
            if sync_runtime.is_hub() and e.node_uuid:
                raise AppError('分店登錄的費用請在該分店修改', 403)
            if action == 'edit':
                pnl_service.save_expense(e, f)
                flash('✅ 已更新費用', 'success')
            else:
                e.is_deleted = True
                flash(f'✅ 已刪除「{e.name}」', 'success')
            record_staff_audit('EXPENSE', details=f"{'修改' if action == 'edit' else '刪除'}費用 {e.name} ${e.amount}")
        elif action == 'copy_fixed':
            n = pnl_service.copy_fixed(g.tenant_id, month, current_user.username)
            flash(f'✅ 已從上個月複製 {n} 筆固定費用' if n else '上個月沒有尚未複製的固定費用', 'success' if n else 'info')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('pnl.expenses', month=month))
    rows = pnl_service.expenses(g.tenant_id, start, end)
    totals = {k: sum(e.amount for e in rows if e.category == k) for k in EXPENSE_CATEGORIES}
    names = {}
    if sync_runtime.is_hub():
        from app.routes.sync_admin import node_name_map
        names = node_name_map()
    return render_template('admin_expenses.html', rows=rows, month=month, start=start, end=end, totals=totals,
                           total=sum(totals.values()), categories=EXPENSE_CATEGORIES, node_names=names,
                           is_hub=sync_runtime.is_hub(), today=date.today())


@pnl_bp.route('/admin/pnl', endpoint='statement')
@permission_required('reports')
def statement():
    if request.args.get('start') and request.args.get('end'):
        try:
            start, end = date.fromisoformat(request.args['start']), date.fromisoformat(request.args['end'])
        except ValueError:
            raise AppError('日期格式錯誤')
        if end < start:
            start, end = end, start
        month = f'{start:%Y-%m}'
    else:
        month, start, end = _month()
    if (end - start).days > 400:
        raise AppError('查詢區間最長 400 天')
    if not (pnl_service.MIN_YEAR <= start.year and end.year <= pnl_service.MAX_YEAR):
        raise AppError(f'請選擇 {pnl_service.MIN_YEAR} ~ {pnl_service.MAX_YEAR} 年的日期')
    node = request.args.get('node') or None
    cur = pnl_service.statement(g.tenant_id, start, end, node)
    pstart, pend = pnl_service.previous_period(start, end)
    prev = pnl_service.statement(g.tenant_id, pstart, pend, node)
    months = pnl_service.trend(g.tenant_id, 6, node)
    if request.args.get('format') == 'xlsx':
        return send_file(pnl_service.to_excel(cur, prev, months, g.tenant.name), as_attachment=True,
                         download_name=f'損益表_{start}_{end}.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    from app.models.sync import SyncNode
    nodes = SyncNode.query.order_by(SyncNode.id).all() if sync_runtime.is_hub() else []
    return render_template('admin_pnl.html', cur=cur, prev=prev, months=months, month=month, node=node, nodes=nodes,
                           rows=pnl_service.STATEMENT_ROWS, fee_labels=pnl_service.FEE_LABELS,
                           categories=EXPENSE_CATEGORIES, max_abs=max([abs(m['net']) for m in months] + [1]))


@pnl_bp.route('/admin/cost-history', endpoint='cost_history')
@permission_required('reports')
def cost_history():
    days = max(1, min(365, safe_int(request.args.get('days'), 30)))
    kind = request.args.get('kind') if request.args.get('kind') in ('ingredient', 'menu') else None
    return render_template('admin_cost_history.html', days=days, kind=kind,
                           impact=pnl_service.cost_impact(g.tenant_id, days),
                           history=pnl_service.history(g.tenant_id, kind, max(days, 90)), labels=FIELD_LABELS)
