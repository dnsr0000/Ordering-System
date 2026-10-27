"""後台：供應商、採購單 (建議採購 → 送出 → 收貨入庫)、備料預測 (具「原物料」權限)"""
from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, PurchaseOrder, Supplier
from app.services import forecast_service, purchasing_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.utils import safe_float, safe_int

purchasing_bp = Blueprint('purchasing', __name__)


def _supplier(sid):
    sup = db.session.get(Supplier, safe_int(sid, 0))
    if sup is None or sup.tenant_id != g.tenant_id:
        raise AppError('查無此供應商', 404)
    return sup


def _po(po_id):
    po = db.session.get(PurchaseOrder, safe_int(po_id, 0))
    if po is None or po.tenant_id != g.tenant_id:
        raise AppError('查無此採購單', 404)
    return po


def _supplier_form(sup, f):
    name = (f.get('name') or '').strip()[:60]
    if not name:
        raise AppError('請輸入供應商名稱')
    dup = Supplier.query.filter(Supplier.name == name, Supplier.id != (sup.id or 0)).first()
    if dup is not None:
        raise AppError(f'供應商「{name}」已存在', 409)
    sup.name = name
    sup.contact = (f.get('contact') or '').strip()[:60]
    sup.phone = (f.get('phone') or '').strip()[:30]
    email = (f.get('email') or '').strip()[:120]
    if email and '@' not in email:
        raise AppError('Email 格式不正確')
    sup.email = email
    sup.lead_days = min(60, max(0, safe_int(f.get('lead_days'), 1)))
    sup.note = (f.get('note') or '').strip()[:200]


@purchasing_bp.route('/admin/purchasing', methods=['GET', 'POST'], endpoint='index')
@permission_required('inventory')
def index():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'add_supplier':
            sup = Supplier(tenant_id=g.tenant_id)
            _supplier_form(sup, f)
            db.session.add(sup)
            flash(f'✅ 已新增供應商「{sup.name}」', 'success')
        elif action == 'edit_supplier':
            sup = _supplier(f.get('supplier_id'))
            _supplier_form(sup, f)
            sup.is_active = bool(f.get('is_active'))
            flash('✅ 已更新供應商', 'success')
        elif action == 'suggest':
            created = purchasing_service.create_suggested_orders(g.tenant_id, current_user.username)
            if not created:
                flash('目前沒有需要補貨的原料 (或已在未收貨的採購單中)', 'info')
            else:
                record_staff_audit('PO_SUGGEST', details=f'依低庫存建立 {len(created)} 張採購單草稿')
                flash(f'✅ 已依供應商建立 {len(created)} 張採購單草稿，請確認數量後送出', 'success')
        elif action == 'new_po':
            sup = _supplier(f.get('supplier_id')) if f.get('supplier_id') else None
            po = PurchaseOrder(tenant_id=g.tenant_id, supplier_id=sup.id if sup else None, created_by=current_user.username,
                               expected_on=date.today() + timedelta(days=sup.lead_days if sup else 1))
            db.session.add(po)
            db.session.commit()
            return redirect(url_for('purchasing.po', po_id=po.id))
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('purchasing.index'))
    orders = PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).limit(100).all()
    return render_template('admin_purchasing.html', suppliers=Supplier.query.order_by(Supplier.name).all(),
                           orders=orders, suggestions=purchasing_service.suggestions(),
                           labels=purchasing_service.STATUS_LABELS)


@purchasing_bp.route('/admin/purchasing/po/<int:po_id>', methods=['GET', 'POST'], endpoint='po')
@permission_required('inventory')
def po(po_id):
    order = _po(po_id)
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'save':
            qty = {ln.id: safe_float(f.get(f'qty__{ln.id}'), ln.quantity) for ln in order.lines}
            cost = {ln.id: safe_float(f.get(f'cost__{ln.id}'), None) for ln in order.lines}
            purchasing_service.update_lines(order, qty, cost, safe_int(f.get('add_ingredient_id'), 0),
                                            safe_float(f.get('add_qty'), 0))
            if order.status == 'draft':
                try:
                    order.expected_on = date.fromisoformat(f.get('expected_on')) if f.get('expected_on') else None
                except ValueError:
                    raise AppError('預計到貨日格式不正確')
                order.note = (f.get('note') or '').strip()[:200]
                if 'supplier_id' in f:
                    order.supplier_id = _supplier(f.get('supplier_id')).id if f.get('supplier_id') else None
            flash('✅ 已儲存', 'success')
        elif action == 'send':
            emailed = purchasing_service.send(order, current_user.username)
            record_staff_audit('PO_SEND', details=f'送出採購單 {order.number}')
            flash(f'✅ 採購單已送出{"，並寄給供應商" if emailed else ""}', 'success')
        elif action == 'receive':
            received = {}
            for ln in order.lines:
                exp = None
                if f.get(f'exp__{ln.id}'):
                    try:
                        exp = date.fromisoformat(f.get(f'exp__{ln.id}'))
                    except ValueError:
                        raise AppError('有效期限格式不正確')
                received[ln.id] = (safe_float(f.get(f'recv__{ln.id}'), ln.quantity), exp)
            purchasing_service.receive(order, received, current_user.username)
            record_staff_audit('PO_RECEIVE', details=f'採購單 {order.number} 收貨入庫')
            flash('✅ 已收貨入庫 (已建立批號)', 'success')
        elif action == 'cancel':
            purchasing_service.cancel(order)
            record_staff_audit('PO_CANCEL', details=f'取消採購單 {order.number}')
            flash('已取消採購單', 'info')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('purchasing.po', po_id=order.id))
    return render_template('admin_po.html', po=order, labels=purchasing_service.STATUS_LABELS,
                           suppliers=Supplier.query.filter_by(is_active=True).order_by(Supplier.name).all(),
                           ingredients=Ingredient.query.filter_by(is_active=True).order_by(Ingredient.name).all())


@purchasing_bp.route('/admin/purchasing/po/<int:po_id>/print', endpoint='po_print')
@permission_required('inventory')
def po_print(po_id):
    return render_template('admin_po_print.html', po=_po(po_id), tenant=g.tenant)


@purchasing_bp.route('/admin/forecast', endpoint='forecast')
@permission_required('inventory')
def forecast():
    try:
        target = date.fromisoformat(request.args.get('date') or '')
    except ValueError:
        target = date.today() + timedelta(days=1)
    weeks = min(12, max(1, request.args.get('weeks', 4, type=int) or 4))
    return render_template('admin_forecast.html', f=forecast_service.forecast(target, weeks), weeks=weeks)
