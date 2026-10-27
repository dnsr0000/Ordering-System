"""
後台：電子發票、作廢與退款紀錄、交班與日結。
"""
import json
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus, Refund
from app.models.shift import CashShift
from app.services import einvoice_ecpay, payment_service, settings_service, shift_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.utils import role_required, safe_int

invoices_bp = Blueprint('invoices', __name__)
refunds_bp = Blueprint('refunds', __name__)
shifts_bp = Blueprint('shifts', __name__)
MANAGERS = ('owner', 'manager')

INVOICE_STATUS_LABELS = {
    InvoiceStatus.WAITING: ('等待付款', 'secondary'), InvoiceStatus.PENDING: ('待開立', 'warning'),
    'issuing': ('開立中', 'info'), InvoiceStatus.ISSUED: ('已開立', 'success'), InvoiceStatus.VOID: ('已作廢', 'dark'),
    InvoiceStatus.ALLOWANCE: ('已折讓', 'info'), InvoiceStatus.FAILED: ('開立失敗', 'danger'),
    InvoiceStatus.SKIPPED: ('不開立', 'secondary'),
}
REFUND_STATUS_LABELS = {'done': ('已退款', 'success'), 'manual': ('需人工退款', 'warning'),
                        'pending': ('處理中', 'info'), 'failed': ('退款失敗', 'danger')}


def _date_range(default_days=7):
    try:
        start = date.fromisoformat(request.args.get('start') or '')
    except ValueError:
        start = date.today() - timedelta(days=default_days - 1)
    try:
        end = date.fromisoformat(request.args.get('end') or '')
    except ValueError:
        end = date.today()
    if end < start:
        start, end = end, start
    return start, end


# ==============================================================================
# 電子發票
# ==============================================================================
@invoices_bp.route('/admin/invoices', endpoint='list_invoices')
@role_required(*MANAGERS)
def list_invoices():
    start, end = _date_range(30)
    status = request.args.get('status') or ''
    q = Invoice.query.filter(Invoice.created_at >= datetime.combine(start, datetime.min.time()),
                             Invoice.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time()))
    if status:
        q = q.filter(Invoice.status == status)
    invoices = q.order_by(Invoice.id.desc()).limit(500).all()
    counts = dict(db.session.query(Invoice.status, db.func.count(Invoice.id)).group_by(Invoice.status).all())
    return render_template('admin_invoices.html', invoices=invoices, labels=INVOICE_STATUS_LABELS, status=status,
                           start=start, end=end, counts=counts, cfg=settings_service.get('einvoice'),
                           total=sum(i.amount for i in invoices if i.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE)))


def _invoice(invoice_id):
    inv = db.session.get(Invoice, invoice_id)
    if inv is None or inv.tenant_id != g.tenant_id:
        raise AppError('查無此發票', 404)
    return inv


@invoices_bp.route('/admin/invoices/<int:invoice_id>/action', methods=['POST'], endpoint='invoice_action')
@role_required(*MANAGERS)
def invoice_action(invoice_id):
    inv = _invoice(invoice_id)
    action = request.form.get('action')
    back = request.form.get('back') or url_for('invoices.list_invoices')
    if not back.startswith('/') or back.startswith('//'):
        back = url_for('invoices.list_invoices')
    if action == 'retry':
        if inv.status == InvoiceStatus.FAILED:
            inv.status, inv.attempts = InvoiceStatus.PENDING, 0
            db.session.commit()
        status = einvoice_ecpay.issue_invoice(inv.id)
        flash('✅ 已開立發票' if status == InvoiceStatus.ISSUED else f'❌ 開立未成功：{inv.last_error}',
              'success' if status == InvoiceStatus.ISSUED else 'danger')
    elif action == 'void':
        reason = (request.form.get('reason') or '').strip() or '作廢'
        status = einvoice_ecpay.void_or_allowance(inv.id, reason)
        record_staff_audit('INVOICE_VOID', target_order_id=inv.order_id,
                           details=f'作廢 / 折讓發票 {inv.invoice_no or inv.relate_number}：{reason}')
        db.session.commit()
        flash(f'發票狀態：{INVOICE_STATUS_LABELS.get(status, (status,))[0]} {inv.last_error or ""}',
              'success' if status in (InvoiceStatus.VOID, InvoiceStatus.ALLOWANCE) else 'warning')
    elif action == 'allowance':
        amount = safe_int(request.form.get('amount'), 0)
        status = einvoice_ecpay.allowance(inv.id, amount, request.form.get('reason') or '折讓')
        record_staff_audit('INVOICE_ALLOWANCE', target_order_id=inv.order_id,
                           details=f'發票 {inv.invoice_no} 折讓 ${amount}')
        db.session.commit()
        flash(f"折讓結果：{INVOICE_STATUS_LABELS.get(status, (status,))[0]} {inv.last_error or ''}", 'info')
    elif action == 'update_carrier':
        if inv.status not in (InvoiceStatus.PENDING, InvoiceStatus.FAILED, InvoiceStatus.WAITING):
            raise AppError('已開立的發票不可修改載具，請作廢後重開')
        fields = einvoice_ecpay.normalize_invoice_info({
            'type': request.form.get('type'), 'carrier': request.form.get('carrier'),
            'love_code': request.form.get('love_code'), 'buyer_id': request.form.get('buyer_id'),
            'buyer_name': request.form.get('buyer_name'), 'email': request.form.get('email')},
            settings_service.get('einvoice'))
        for k, v in fields.items():
            setattr(inv, k, v)
        if inv.status == InvoiceStatus.FAILED:
            inv.status, inv.attempts = InvoiceStatus.PENDING, 0
        db.session.commit()
        flash('✅ 已更新發票資料，請按「重送」開立', 'success')
    else:
        raise AppError('不支援的操作')
    return redirect(back)


@invoices_bp.route('/admin/invoices/<int:invoice_id>/print', endpoint='invoice_print')
@role_required(*MANAGERS, 'staff')
def invoice_print(invoice_id):
    """導向綠界的發票證明聯列印頁"""
    return redirect(einvoice_ecpay.print_url(_invoice(invoice_id).id))


@invoices_bp.route('/admin/orders/<int:order_id>/invoice', methods=['POST'], endpoint='issue_for_order')
@role_required(*MANAGERS, 'staff')
def issue_for_order(order_id):
    """補開發票 (結帳時未開立、或關閉自動開立時)"""
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != g.tenant_id:
        raise AppError('查無此訂單', 404)
    if not settings_service.get('einvoice').get('enabled'):
        raise AppError('尚未啟用電子發票')
    data = request.get_json(silent=True) or request.form.to_dict()
    existing = Invoice.query.filter(Invoice.order_id == order.id, Invoice.status != InvoiceStatus.VOID).first()
    if existing is not None and existing.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
        raise AppError(f'此訂單已開立發票 {existing.invoice_no}', 409)
    inv = existing or einvoice_ecpay.create_invoice(order, data)
    db.session.commit()
    status = einvoice_ecpay.issue_invoice(inv.id)
    return jsonify({'success': status == InvoiceStatus.ISSUED, 'status': status, 'invoice_no': inv.invoice_no,
                    'message': inv.last_error or '已開立'})


# ==============================================================================
# 作廢與退款
# ==============================================================================
@refunds_bp.route('/admin/refunds', endpoint='list_refunds')
@role_required(*MANAGERS)
def list_refunds():
    start, end = _date_range(30)
    status = request.args.get('status') or ''
    q = Refund.query.filter(Refund.created_at >= datetime.combine(start, datetime.min.time()),
                            Refund.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time()))
    if status:
        q = q.filter(Refund.status == status)
    refunds = q.order_by(Refund.id.desc()).limit(500).all()
    # 線上付款被取消、尚未建立退款紀錄的舊資料 (需退款)
    pending_online = Order.query.filter(Order.payment_status == 'refund_required').order_by(Order.id.desc()).limit(100).all()
    by_method = {}
    for r in refunds:
        by_method[r.method] = by_method.get(r.method, 0) + r.amount
    return render_template('admin_refunds.html', refunds=refunds, labels=REFUND_STATUS_LABELS, status=status,
                           start=start, end=end, by_method=by_method, total=sum(r.amount for r in refunds),
                           pending_online=pending_online,
                           ecpay_refundable={o.id for o in pending_online if payment_service.ecpay_integrated(o)})


@refunds_bp.route('/admin/refunds/<int:refund_id>/action', methods=['POST'], endpoint='refund_action')
@role_required(*MANAGERS)
def refund_action(refund_id):
    refund = db.session.get(Refund, refund_id)
    if refund is None or refund.tenant_id != g.tenant_id:
        raise AppError('查無退款紀錄', 404)
    action = request.form.get('action')
    if action == 'done':
        payment_service.mark_refund_done(refund.id, current_user.username, request.form.get('note') or '')
        record_staff_audit('REFUND_DONE', target_order_id=refund.order_id,
                           details=f'退款 #{refund.id} (${refund.amount} {refund.method}) 標記為已完成')
        db.session.commit()
        flash('✅ 已標記完成退款', 'success')
    elif action == 'retry':
        refund.status = 'failed'
        db.session.commit()
        status = payment_service.process_refund(refund.id)
        flash('✅ 線上退款成功' if status == 'done' else f'❌ 退款失敗：{refund.provider_result}',
              'success' if status == 'done' else 'danger')
    else:
        raise AppError('不支援的操作')
    return redirect(url_for('refunds.list_refunds'))


@refunds_bp.route('/admin/refunds/order/<int:order_id>/done', methods=['POST'], endpoint='online_refund_done')
@role_required(*MANAGERS)
def online_refund_done(order_id):
    """線上付款 (綠界) 取消後，已在綠界後台退刷：標記為已退款"""
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != g.tenant_id or order.payment_status != 'refund_required':
        raise AppError('查無需退款的訂單', 404)
    refund = Refund.query.filter_by(order_id=order.id).filter(Refund.status != 'done').first()
    if refund is not None:
        payment_service.mark_refund_done(refund.id, current_user.username)
    order.payment_status = 'refunded'
    record_staff_audit('REFUND_DONE', target_order_id=order.id, details=f'訂單 #{order.id} 已於金流後台完成退款')
    db.session.commit()
    flash(f'✅ 訂單 #{order.id} 已標記為已退款', 'success')
    return redirect(url_for('refunds.list_refunds'))


@refunds_bp.route('/admin/refunds/order/<int:order_id>/ecpay', methods=['POST'], endpoint='online_refund_ecpay')
@role_required(*MANAGERS)
def online_refund_ecpay(order_id):
    """線上端：已取消的綠界信用卡 / Apple Pay 訂單直接向綠界退刷 (自動退刷失敗後也可在這裡重試)"""
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != g.tenant_id or order.payment_status != 'refund_required':
        raise AppError('查無需退款的訂單', 404)
    if not payment_service.ecpay_integrated(order):
        raise AppError('此訂單不是綠界信用卡 / Apple Pay 付款，請到對應的金流後台退款')
    ok, msg = payment_service.auto_refund_online_order(order.id)
    flash(f'✅ {msg}' if ok else f'❌ 綠界退刷失敗：{msg}', 'success' if ok else 'danger')
    return redirect(url_for('refunds.list_refunds'))


# ==============================================================================
# 交班與日結
# ==============================================================================
@shifts_bp.route('/admin/shifts', endpoint='list_shifts')
@permission_required('cash_drawer')
def list_shifts():
    shifts = CashShift.query.order_by(CashShift.id.desc()).limit(100).all()
    try:
        day = date.fromisoformat(request.args.get('day') or '')
    except ValueError:
        day = date.today()
    current = shift_service.current_shift(g.tenant_id)
    return render_template('admin_shifts.html', shifts=shifts, current=current,
                           current_report=shift_service.shift_report(current) if current else None,
                           daily=shift_service.daily_report(g.tenant_id, day), day=day)


@shifts_bp.route('/admin/shifts/<int:shift_id>', endpoint='shift_detail')
@permission_required('cash_drawer')
def shift_detail(shift_id):
    shift = db.session.get(CashShift, shift_id)
    if shift is None or shift.tenant_id != g.tenant_id:
        raise AppError('查無此班別', 404)
    report = json.loads(shift.report) if shift.report else shift_service.shift_report(shift)
    detail = json.loads(shift.cash_detail) if shift.cash_detail else {}
    return render_template('admin_shift_detail.html', shift=shift, report=report, detail=detail,
                           lines=shift_service.report_lines(report))


@shifts_bp.route('/admin/shifts/<int:shift_id>/print', methods=['POST'], endpoint='shift_print')
@permission_required('cash_drawer')
def shift_print(shift_id):
    from app.services import print_service
    shift = db.session.get(CashShift, shift_id)
    if shift is None or shift.tenant_id != g.tenant_id:
        raise AppError('查無此班別', 404)
    report = json.loads(shift.report) if shift.report else shift_service.shift_report(shift)
    print_service.print_text_document(g.tenant_id, '交班報表', shift_service.report_lines(report))
    flash('✅ 已送出列印', 'success')
    return redirect(url_for('shifts.shift_detail', shift_id=shift.id))
