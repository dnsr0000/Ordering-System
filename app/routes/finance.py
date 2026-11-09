"""
後台：電子發票、作廢與退款紀錄、交班與日結。
"""
import json
from datetime import date

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.models.enums import (DELIVERY_SOURCES, MANAGER_ROLES, POS_ROLES, OrderSource, OrderStatus,
                              PaymentMethod)
from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus, Refund
from app.models.shift import CashShift
from app.services import einvoice_ecpay, payment_service, settings_service, shift_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.sync import runtime as sync_runtime
from app.utils import day_bounds, get_own, parse_date_range, role_required, safe_int

einvoice_enabled = settings_service.flag('einvoice')
invoices_bp = Blueprint('invoices', __name__)
refunds_bp = Blueprint('refunds', __name__)
shifts_bp = Blueprint('shifts', __name__)

INVOICE_STATUS_LABELS = {
    InvoiceStatus.WAITING: ('等待付款', 'secondary'), InvoiceStatus.PENDING: ('待開立', 'warning'),
    'issuing': ('開立中', 'info'), InvoiceStatus.ISSUED: ('已開立', 'success'), InvoiceStatus.VOID: ('已作廢', 'dark'),
    InvoiceStatus.ALLOWANCE: ('已折讓', 'info'), InvoiceStatus.FAILED: ('開立失敗', 'danger'),
    InvoiceStatus.SKIPPED: ('不開立', 'secondary'),
}
REFUND_STATUS_LABELS = {'done': ('已退款', 'success'), 'manual': ('需人工退款', 'warning'),
                        'pending': ('處理中', 'info'), 'processing': ('退款中', 'info'), 'failed': ('退款失敗', 'danger')}
REMOTE_MESSAGE = '這筆資料屬於分店，請在分店的後台處理 (線上端修改不會同步回分店)'
HUB_ECPAY_MESSAGE = '此訂單為線上信用卡 / Apple Pay 付款，由總部 (線上端) 向綠界退刷，請聯絡總部處理'


def _remote_order(order):
    """線上端：分店上傳的分店自有訂單 (線上點餐由線上端收款，不算)。這類訂單的發票 / 退款由分店處理"""
    return (sync_runtime.is_hub() and order is not None and order.node_uuid is not None
            and order.order_source != OrderSource.ONLINE)


def _hub_ecpay_order(order):
    """分店：線上端以綠界收款的線上信用卡 / Apple Pay 訂單 (退刷由線上端處理，分店不可標記已退款)"""
    return (sync_runtime.is_node() and order is not None and order.order_source == OrderSource.ONLINE
            and bool(order.payment_trade_no) and order.payment_method in (PaymentMethod.CREDIT_CARD,
                                                                           PaymentMethod.APPLE_PAY))


# ==============================================================================
# 電子發票
# ==============================================================================
@invoices_bp.route('/admin/invoices', endpoint='list_invoices')
@role_required(*MANAGER_ROLES)
def list_invoices():
    start, end = parse_date_range(default_days=30)
    status = request.args.get('status') or ''
    t0, t1 = day_bounds(start, end)
    q = Invoice.query.filter(Invoice.created_at >= t0, Invoice.created_at < t1)
    if status:
        q = q.filter(Invoice.status == status)
    invoices = q.order_by(Invoice.id.desc()).limit(500).all()
    counts = dict(db.session.query(Invoice.status, db.func.count(Invoice.id)).group_by(Invoice.status).all())
    ids = [i.id for i in invoices]
    remote_ids = set()
    local = einvoice_ecpay._local_invoice_clause()   # 線上端：分店上傳的發票由分店開立 / 作廢 (這裡唯讀)
    if local is not None and ids:
        remote_ids = set(ids) - {i for (i,) in db.session.query(Invoice.id).filter(Invoice.id.in_(ids), local)}
    issued = [i for i in invoices if i.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE)]
    total = sum(i.amount for i in issued)
    allowance_total = sum(i.allowance_amount or 0 for i in issued)
    return render_template('admin_invoices.html', invoices=invoices, labels=INVOICE_STATUS_LABELS, status=status,
                           start=start, end=end, counts=counts, cfg=settings_service.get('einvoice'),
                           total=total, allowance_total=allowance_total, net_total=total - allowance_total,
                           remote_ids=remote_ids, reissuable=_reissuable(invoices, remote_ids))


def _reissuable(invoices, remote_ids):
    """可「重開」的已作廢發票 id：訂單仍有效 (未取消、已收款) 且沒有其他未作廢的發票 (同一訂單只列最新一張)"""
    latest = {}
    for inv in invoices:   # 依 id 由新到舊
        if inv.status == InvoiceStatus.VOID and inv.order_id and inv.id not in remote_ids:
            latest.setdefault(inv.order_id, inv.id)
    if not latest:
        return set()
    active = {oid for (oid,) in db.session.query(Invoice.order_id).filter(
        Invoice.order_id.in_(list(latest)), Invoice.status != InvoiceStatus.VOID)}
    orders = Order.query.filter(Order.id.in_(list(latest))).all()
    return {latest[o.id] for o in orders
            if o.id not in active and o.status != OrderStatus.CANCELLED and o.is_paid}


def _invoice(invoice_id):
    inv = get_own(Invoice, invoice_id, '發票')
    return inv


def _ensure_order_not_cancelled(inv):
    """開立失敗的發票改回待開立前：訂單已取消 (已退款) 就不可再開立"""
    if inv.order is not None and inv.order.status == OrderStatus.CANCELLED:
        raise AppError('訂單已取消，不可再開立此發票', 409)


@invoices_bp.route('/admin/invoices/<int:invoice_id>/action', methods=['POST'], endpoint='invoice_action')
@role_required(*MANAGER_ROLES)
def invoice_action(invoice_id):
    inv = _invoice(invoice_id)
    action = request.form.get('action')
    back = request.form.get('back') or url_for('invoices.list_invoices')
    if not back.startswith('/') or back.startswith('//'):
        back = url_for('invoices.list_invoices')
    if not einvoice_ecpay.is_local_invoice(inv):
        # 線上端：分店上傳的發票由分店開立 / 作廢；線上端改了不會同步回分店 (也不可用總部的特店資料開立)
        raise AppError(REMOTE_MESSAGE, 409)
    if action == 'retry':
        if inv.status == InvoiceStatus.FAILED:
            _ensure_order_not_cancelled(inv)
            inv.status, inv.attempts = InvoiceStatus.PENDING, 0
            db.session.commit()
        status = einvoice_ecpay.issue_invoice(inv.id)
        flash('已開立發票' if status == InvoiceStatus.ISSUED else f'開立未成功：{inv.last_error}',
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
        if inv.status == InvoiceStatus.FAILED:
            _ensure_order_not_cancelled(inv)
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
        flash('已更新發票資料，請按「重送」開立', 'success')
    elif action == 'reissue':
        # 作廢後重開 (例如統編 / 載具打錯)：以新的載具資料替同一張訂單開立新發票
        if inv.status != InvoiceStatus.VOID or inv.order is None:
            raise AppError('只有已作廢的訂單發票可以重開', 409)
        new = _invoice_for_order(inv.order, request.form.to_dict())
        db.session.commit()
        status = einvoice_ecpay.issue_invoice(new.id)
        record_staff_audit('INVOICE_REISSUE', target_order_id=inv.order_id,
                           details=f'作廢發票 {inv.invoice_no or inv.relate_number} 重開')
        db.session.commit()
        flash('已重開發票' if status == InvoiceStatus.ISSUED else f'重開未成功：{_invoice_message(new, status)}',
              'success' if status == InvoiceStatus.ISSUED else 'danger')
    else:
        raise AppError('不支援的操作')
    return redirect(back)


@invoices_bp.route('/admin/invoices/<int:invoice_id>/print', endpoint='invoice_print')
@role_required(*POS_ROLES)
def invoice_print(invoice_id):
    """導向綠界的發票證明聯列印頁"""
    return redirect(einvoice_ecpay.print_url(_invoice(invoice_id).id))


@invoices_bp.route('/admin/orders/<int:order_id>/invoice', methods=['POST'], endpoint='issue_for_order')
@role_required(*POS_ROLES)
def issue_for_order(order_id):
    """補開發票 (結帳時未開立、或關閉自動開立時)"""
    order = get_own(Order, order_id, '訂單')
    data = request.get_json(silent=True) or request.form.to_dict()
    inv = _invoice_for_order(order, data)
    db.session.commit()
    status = einvoice_ecpay.issue_invoice(inv.id)
    return jsonify({'success': status == InvoiceStatus.ISSUED, 'status': status, 'invoice_no': inv.invoice_no,
                    'message': _invoice_message(inv, status)})


def _invoice_for_order(order, data):
    """
    補開 / 重開訂單發票前的檢查，回傳要開立的發票 (沿用待開立的發票，或新建立一張)。由呼叫端 commit。
    已取消、未收款、外送平台、已併入桌位結帳發票、已有發票 (開立中 / 等待付款 / 不開立 / 開立失敗) 的訂單不可補開；
    金額為訂單金額扣掉已退款金額。
    """
    if not einvoice_enabled():
        raise AppError('尚未啟用電子發票')
    if _remote_order(order):
        raise AppError(REMOTE_MESSAGE, 409)
    if order.status == OrderStatus.CANCELLED:
        raise AppError('已取消的訂單不可開立發票', 409)
    if not order.is_paid:
        raise AppError('訂單尚未完成收款，收款後才可開立發票', 409)
    if order.order_source in DELIVERY_SOURCES:
        raise AppError('外送平台訂單由平台開立發票', 409)
    if order.settlement_id:
        combined = Invoice.query.filter(Invoice.settlement_id == order.settlement_id, Invoice.order_id.is_(None),
                                        Invoice.status != InvoiceStatus.VOID).first()
        if combined is not None:
            raise AppError(f'此訂單已併入桌位結帳發票 {combined.invoice_no or combined.relate_number}', 409)
    existing = Invoice.query.filter(Invoice.order_id == order.id, Invoice.status != InvoiceStatus.VOID) \
        .order_by(Invoice.id.desc()).first()
    if existing is not None:
        if existing.status == InvoiceStatus.PENDING:
            return existing
        if existing.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
            raise AppError(f'此訂單已開立發票 {existing.invoice_no}', 409)
        if existing.status == InvoiceStatus.FAILED:
            raise AppError('此訂單的發票開立失敗，請到「電子發票」修改載具後重送', 409)
        label = INVOICE_STATUS_LABELS.get(existing.status, (existing.status,))[0]
        raise AppError(f'此訂單已有發票 (目前為「{label}」)，不需補開', 409)
    amount = order.refundable_amount
    if amount <= 0:
        raise AppError('訂單已全額退款，不需開立發票', 409)
    return einvoice_ecpay.create_invoice(order, data, amount=amount)


def _invoice_message(inv, status):
    if status == InvoiceStatus.ISSUED:
        return '已開立'
    return inv.last_error or f'發票狀態：{INVOICE_STATUS_LABELS.get(status, (status,))[0]}'


# ==============================================================================
# 作廢與退款
# ==============================================================================
@refunds_bp.route('/admin/refunds', endpoint='list_refunds')
@role_required(*MANAGER_ROLES)
def list_refunds():
    start, end = parse_date_range(default_days=30)
    status = request.args.get('status') or ''
    t0, t1 = day_bounds(start, end)
    q = Refund.query.filter(Refund.created_at >= t0, Refund.created_at < t1)
    statuses = [s for s in status.split(',') if s]   # 可用逗號指定多個狀態 (例如總覽的 manual,failed)
    if statuses:
        q = q.filter(Refund.status.in_(statuses))
    refunds = q.order_by(Refund.id.desc()).limit(500).all()
    # 線上付款被取消、尚未建立退款紀錄的舊資料 (需退款)
    pending_online = Order.query.filter(Order.payment_status == 'refund_required').order_by(Order.id.desc()).limit(100).all()
    by_method = {}
    for r in refunds:
        by_method[r.method] = by_method.get(r.method, 0) + r.amount
    # 線上端：分店自有訂單的退款由分店處理 (唯讀)；分店：線上信用卡 / Apple Pay 訂單由線上端向綠界退刷
    remote_orders = {o.id for o in pending_online if _remote_order(o)}
    hub_orders = {o.id for o in pending_online if _hub_ecpay_order(o)}
    remote_refunds, hub_refunds = set(), set()
    if sync_runtime.is_hub() or sync_runtime.is_node():
        remote_refunds = {r.id for r in refunds if _remote_order(r.order)}
        hub_refunds = {r.id for r in refunds if _hub_ecpay_order(r.order)}
    return render_template('admin_refunds.html', refunds=refunds, labels=REFUND_STATUS_LABELS, status=status,
                           start=start, end=end, by_method=by_method, total=sum(r.amount for r in refunds),
                           pending_online=pending_online, remote_orders=remote_orders, hub_orders=hub_orders,
                           remote_refunds=remote_refunds, hub_refunds=hub_refunds,
                           ecpay_refundable={o.id for o in pending_online if payment_service.ecpay_integrated(o)})


@refunds_bp.route('/admin/refunds/<int:refund_id>/action', methods=['POST'], endpoint='refund_action')
@role_required(*MANAGER_ROLES)
def refund_action(refund_id):
    refund = get_own(Refund, refund_id, '退款紀錄')
    action = request.form.get('action')
    if _remote_order(refund.order):
        raise AppError(REMOTE_MESSAGE, 409)
    if action == 'done' and _hub_ecpay_order(refund.order):
        raise AppError(HUB_ECPAY_MESSAGE, 409)
    if action == 'done':
        payment_service.mark_refund_done(refund.id, current_user.username, request.form.get('note') or '')
        record_staff_audit('REFUND_DONE', target_order_id=refund.order_id,
                           details=f'退款 #{refund.id} (${refund.amount} {refund.method}) 標記為已完成')
        db.session.commit()
        flash('已標記完成退款', 'success')
    elif action == 'retry':
        if refund.status != 'failed':
            # 只有失敗的才重試：已完成 / 處理中的再退一次會重複退款 (連按兩下、重新送出表單)
            raise AppError(f'這筆退款目前是「{refund.status}」，不需要重試', 409)
        status = payment_service.process_refund(refund.id)
        if status is None:
            raise AppError('這筆退款正在處理中或已完成，請重新整理頁面', 409)
        flash('線上退款成功' if status == 'done' else f'退款失敗：{refund.provider_result}',
              'success' if status == 'done' else 'danger')
    else:
        raise AppError('不支援的操作')
    return redirect(url_for('refunds.list_refunds'))


@refunds_bp.route('/admin/refunds/order/<int:order_id>/done', methods=['POST'], endpoint='online_refund_done')
@role_required(*MANAGER_ROLES)
def online_refund_done(order_id):
    """線上付款 (綠界) 取消後，已在綠界後台退刷：標記為已退款"""
    order = get_own(Order, order_id, '需退款的訂單')
    if order.payment_status != 'refund_required':
        raise AppError('查無需退款的訂單', 404)
    if _remote_order(order):
        raise AppError(REMOTE_MESSAGE, 409)
    if _hub_ecpay_order(order):
        raise AppError(HUB_ECPAY_MESSAGE, 409)
    open_refunds = Refund.query.filter_by(order_id=order.id).filter(Refund.status != 'done').order_by(Refund.id).all()
    if any(r.status == 'processing' for r in open_refunds):
        raise AppError('這張訂單有退款正在向金流處理中，請稍後重新整理頁面', 409)
    # 已在金流後台 / 櫃檯退完全部款項：這張訂單所有未完成的退款紀錄都標記完成 (不是只標第一筆)
    for refund in open_refunds:
        payment_service.mark_refund_done(refund.id, current_user.username)
    order.payment_status = 'refunded'
    record_staff_audit('REFUND_DONE', target_order_id=order.id, details=f'訂單 #{order.id} 已於金流後台完成退款')
    db.session.commit()
    flash(f'訂單 #{order.id} 已標記為已退款', 'success')
    return redirect(url_for('refunds.list_refunds'))


@refunds_bp.route('/admin/refunds/order/<int:order_id>/ecpay', methods=['POST'], endpoint='online_refund_ecpay')
@role_required(*MANAGER_ROLES)
def online_refund_ecpay(order_id):
    """線上端：已取消的綠界信用卡 / Apple Pay 訂單直接向綠界退刷 (自動退刷失敗後也可在這裡重試)"""
    order = get_own(Order, order_id, '需退款的訂單')
    if order.payment_status != 'refund_required':
        raise AppError('查無需退款的訂單', 404)
    if not payment_service.ecpay_integrated(order):
        raise AppError('此訂單不是綠界信用卡 / Apple Pay 付款，請到對應的金流後台退款')
    ok, msg = payment_service.auto_refund_online_order(order.id)
    flash(f'{msg}' if ok else f'❌ 綠界退刷失敗：{msg}', 'success' if ok else 'danger')
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
    shift = get_own(CashShift, shift_id, '班別')
    report = json.loads(shift.report) if shift.report else shift_service.shift_report(shift)
    detail = json.loads(shift.cash_detail) if shift.cash_detail else {}
    return render_template('admin_shift_detail.html', shift=shift, report=report, detail=detail,
                           lines=shift_service.report_lines(report))


@shifts_bp.route('/admin/shifts/<int:shift_id>/print', methods=['POST'], endpoint='shift_print')
@permission_required('cash_drawer')
def shift_print(shift_id):
    from app.services import print_service
    shift = get_own(CashShift, shift_id, '班別')
    report = json.loads(shift.report) if shift.report else shift_service.shift_report(shift)
    print_service.print_text_document(g.tenant_id, '交班報表', shift_service.report_lines(report))
    flash('已送出列印', 'success')
    return redirect(url_for('shifts.shift_detail', shift_id=shift.id))
