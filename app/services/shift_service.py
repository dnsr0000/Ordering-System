"""
交班與日結：
- 開班：輸入零用金 (每台門市主機同時只有一個開著的班)。
- 班中：所有收款 / 退款 / 零用金存入支出都記在 payment_record，並標記所屬班別。
- 關班：盤點現金 (可依面額輸入) → 應有現金 = 零用金 + 現金收款 - 現金退款 ± 零用金異動，計算帳差並保存報表快照。
- 日結：某一天所有班別與訂單的彙總 (付款方式、通路、折扣、退款、品項)。
"""
import json
from datetime import date, datetime, timedelta

from sqlalchemy import func, update

from app.errors import AppError
from app.extensions import db
from app.models.enums import PAYMENT_METHOD_LABELS, OrderStatus, PaymentMethod
from app.models.order import Order, OrderItem
from app.models.payment import PaymentKind, PaymentRecord, Refund
from app.models.shift import CashShift
from app.services import settings_service
from app.services.audit import record_staff_audit

DENOMINATIONS = (1000, 500, 200, 100, 50, 10, 5, 1)


def current_shift(tenant_id):
    return CashShift.query.filter_by(tenant_id=tenant_id, status='open').order_by(CashShift.id.desc()).first()


def open_shift(tenant_id, staff_username, opening_cash, note=''):
    if current_shift(tenant_id) is not None:
        raise AppError('目前已有開著的班別，請先關班', 409)
    opening_cash = int(opening_cash or 0)
    if opening_cash < 0:
        raise AppError('零用金不可為負數')
    from app.services.db_locks import lock_tenant
    lock_tenant(tenant_id)   # 兩台 POS 同時開班：依序處理，新增後再數一次
    shift = CashShift(tenant_id=tenant_id, opened_by=staff_username, opened_at=datetime.now(),
                      opening_cash=opening_cash, note=str(note or '')[:300])
    db.session.add(shift)
    db.session.flush()
    if CashShift.query.filter_by(tenant_id=tenant_id, status='open').count() > 1:
        db.session.rollback()
        raise AppError('另一台 POS 剛開班，目前已有開著的班別，請重新整理', 409)
    record_staff_audit('SHIFT_OPEN', tenant_id=tenant_id, details=f'開班 #{shift.id}，零用金 ${opening_cash}')
    return shift


def cash_movement(tenant_id, staff_username, amount, kind, note):
    """零用金存入 (pay_in) / 支出 (pay_out，例如買冰塊)"""
    from app.services.payment_service import record_payment
    shift = current_shift(tenant_id)
    if shift is None:
        raise AppError('尚未開班，無法登記零用金', 409)
    amount = abs(int(amount or 0))
    if amount <= 0:
        raise AppError('金額必須大於 0')
    if not str(note or '').strip():
        raise AppError('請填寫用途說明')
    signed = amount if kind == PaymentKind.PAY_IN else -amount
    rec = record_payment(None, kind, PaymentMethod.CASH, signed, note=note, staff=staff_username, tenant_id=tenant_id)
    record_staff_audit('CASH_' + kind.upper(), tenant_id=tenant_id,
                       details=f"{'零用金存入' if signed > 0 else '零用金支出'} ${amount}：{note}")
    return rec


def summarize_payments(records):
    """{method: {'sales', 'refunds', 'topups', 'movements', 'net', 'count'}}"""
    out = {}
    for r in records:
        row = out.setdefault(r.method, {'sales': 0, 'refunds': 0, 'topups': 0, 'movements': 0, 'net': 0, 'count': 0})
        if r.kind in (PaymentKind.SALE, PaymentKind.SETTLE):
            row['sales'] += r.amount
            row['count'] += 1
        elif r.kind == PaymentKind.REFUND:
            row['refunds'] += -r.amount
        elif r.kind in (PaymentKind.TOPUP, PaymentKind.GIFT_SALE):
            row['topups'] += r.amount
        else:
            row['movements'] += r.amount
        row['net'] += r.amount
    return out


def shift_report(shift):
    records = PaymentRecord.query.filter_by(shift_id=shift.id).order_by(PaymentRecord.id).all()
    by_method = summarize_payments(records)
    cash = by_method.get(PaymentMethod.CASH, {})
    expected = (shift.opening_cash or 0) + cash.get('net', 0)
    orders = Order.query.filter_by(shift_id=shift.id).all()
    valid = [o for o in orders if o.status != OrderStatus.CANCELLED]
    return {
        'shift_id': shift.id,
        'opened_by': shift.opened_by, 'opened_at': shift.opened_at.strftime('%Y-%m-%d %H:%M'),
        'closed_by': shift.closed_by,
        'closed_at': shift.closed_at.strftime('%Y-%m-%d %H:%M') if shift.closed_at else '',
        'opening_cash': shift.opening_cash or 0,
        'expected_cash': expected,
        'by_method': {PAYMENT_METHOD_LABELS.get(k, k): v for k, v in by_method.items()},
        'cash_sales': cash.get('sales', 0), 'cash_refunds': cash.get('refunds', 0),
        'cash_topups': cash.get('topups', 0), 'cash_movements': cash.get('movements', 0),
        'order_count': len(valid), 'cancelled_count': len(orders) - len(valid),
        'gross': sum(o.subtotal for o in valid), 'discounts': sum(o.discount_amount or 0 for o in valid),
        'service_charge': sum(o.service_charge or 0 for o in valid),
        'net_sales': sum(o.total_price or 0 for o in valid),
        'refund_total': sum(v['refunds'] for v in by_method.values()),
        'movements': [{'at': r.created_at.strftime('%H:%M'), 'amount': r.amount, 'note': r.note, 'staff': r.staff_username}
                      for r in records if r.kind in (PaymentKind.PAY_IN, PaymentKind.PAY_OUT)],
    }


def close_shift(tenant_id, staff_username, counted=None, denominations=None, note=''):
    shift = current_shift(tenant_id)
    if shift is None:
        raise AppError('目前沒有開著的班別', 409)
    detail = {}
    if denominations:
        for d in DENOMINATIONS:
            n = int((denominations or {}).get(str(d)) or 0)
            if n < 0:
                raise AppError('面額張數不可為負數')
            if n:
                detail[str(d)] = n
        counted = sum(int(k) * v for k, v in detail.items())
    if counted is None or str(counted).strip() == '':
        raise AppError('請輸入盤點的現金金額')
    counted = int(counted)
    # 先把班別標記為已關 (取得鎖) 再計算報表：同時結帳的那一筆要嘛已提交而算進報表，要嘛等關班後才寫入、不掛在這個班別
    # (結帳時以 FOR SHARE 讀取開著的班別；SQLite 的寫入鎖讓結帳等到關班提交)
    closed_at = datetime.now()
    claimed = db.session.execute(
        update(CashShift).where(CashShift.id == shift.id, CashShift.status == 'open')
        .values(status='closed', closed_by=staff_username, closed_at=closed_at)
        .execution_options(synchronize_session=False)).rowcount
    if not claimed:
        db.session.rollback()
        raise AppError('此班別已由其他人關班，請重新整理', 409)
    db.session.refresh(shift)
    report = shift_report(shift)
    shift.status = 'closed'
    shift.closed_by, shift.closed_at = staff_username, closed_at
    shift.expected_cash = report['expected_cash']
    shift.counted_cash = counted
    shift.difference = counted - report['expected_cash']
    shift.cash_detail = json.dumps(detail)
    shift.note = '\n'.join(filter(None, [shift.note, str(note or '')[:200]]))[:300]
    report.update(closed_by=shift.closed_by, closed_at=shift.closed_at.strftime('%Y-%m-%d %H:%M'),
                  counted_cash=counted, difference=shift.difference)
    shift.report = json.dumps(report, ensure_ascii=False)
    record_staff_audit('SHIFT_CLOSE', tenant_id=tenant_id,
                       details=f'關班 #{shift.id}：應有現金 ${shift.expected_cash}，實點 ${counted}，帳差 {shift.difference:+d}')
    limit = int(settings_service.get('anomaly', tenant_id).get('shift_diff_limit') or 0)
    if limit and abs(shift.difference) >= limit:   # 帳差過大：提交後立即通知
        from app.services import deferred, monitor_service
        deferred.defer(monitor_service.alert, f'交班帳差過大：#{shift.id} {staff_username} 關班，應有現金 ${shift.expected_cash}、'
                                              f'實點 ${counted}，帳差 {shift.difference:+d} 元', key=f'shift-diff-{shift.id}',
                       tenant_id=tenant_id, level='warning', source='anomaly')
    return shift, report


def report_lines(report):
    """交班報表的列印內容"""
    lines = [('班別', f"#{report['shift_id']}"), ('開班', f"{report['opened_at']} {report['opened_by']}"),
             ('關班', f"{report.get('closed_at') or '-'} {report.get('closed_by') or ''}"), '-',
             ('訂單數', report['order_count']), ('取消單數', report['cancelled_count']),
             ('商品小計', f"${report['gross']}"), ('折扣', f"-${report['discounts']}"),
             ('服務費', f"${report['service_charge']}"), ('營業額', f"${report['net_sales']}"),
             ('退款', f"-${report['refund_total']}"), '-']
    for method, v in report['by_method'].items():
        lines.append((method, f"${v['net']}"))
    lines += ['-', ('零用金', f"${report['opening_cash']}"), ('應有現金', f"${report['expected_cash']}")]
    if 'counted_cash' in report:
        lines += [('實點現金', f"${report['counted_cash']}"), ('帳差', f"{report['difference']:+d}")]
    return lines


def daily_report(tenant_id, day=None):
    """日結：某一天 (訂單建立時間) 的彙總"""
    day = day or date.today()
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    orders = Order.query.filter(Order.tenant_id == tenant_id, Order.created_at >= start, Order.created_at < end).all()
    valid = [o for o in orders if o.status != OrderStatus.CANCELLED]
    records = PaymentRecord.query.filter(PaymentRecord.tenant_id == tenant_id, PaymentRecord.created_at >= start,
                                         PaymentRecord.created_at < end).all()
    refunds = Refund.query.filter(Refund.tenant_id == tenant_id, Refund.created_at >= start, Refund.created_at < end).all()
    by_source = {}
    for o in valid:
        row = by_source.setdefault(o.order_source or 'Kiosk', {'count': 0, 'amount': 0})
        row['count'] += 1
        row['amount'] += o.total_price or 0
    items = db.session.query(OrderItem.item_name, func.sum(OrderItem.quantity), func.sum(OrderItem.quantity * OrderItem.price)) \
        .join(Order, Order.id == OrderItem.order_id) \
        .filter(Order.tenant_id == tenant_id, Order.created_at >= start, Order.created_at < end,
                Order.status != OrderStatus.CANCELLED) \
        .group_by(OrderItem.item_name).order_by(func.sum(OrderItem.quantity).desc()).all()
    shifts = CashShift.query.filter(CashShift.tenant_id == tenant_id, CashShift.opened_at >= start,
                                    CashShift.opened_at < end).order_by(CashShift.id).all()
    return {
        'date': day.isoformat(),
        'order_count': len(valid), 'cancelled_count': len(orders) - len(valid),
        'gross': sum(o.subtotal for o in valid), 'discounts': sum(o.discount_amount or 0 for o in valid),
        'service_charge': sum(o.service_charge or 0 for o in valid),
        'net_sales': sum(o.total_price or 0 for o in valid),
        'unpaid': sum(o.total_price or 0 for o in valid if o.payment_status in ('unpaid', 'pending')),
        'by_method': {PAYMENT_METHOD_LABELS.get(k, k): v for k, v in summarize_payments(records).items()},
        'by_source': by_source,
        'refunds': [{'order_id': r.order_id, 'amount': r.amount, 'method': r.method, 'status': r.status,
                     'reason': r.reason} for r in refunds],
        'refund_total': sum(r.amount for r in refunds),
        'items': [{'name': n, 'qty': int(q or 0), 'amount': int(a or 0)} for n, q, a in items],
        'shifts': [{'id': s.id, 'opened_by': s.opened_by, 'status': s.status, 'opening_cash': s.opening_cash,
                    'expected_cash': s.expected_cash, 'counted_cash': s.counted_cash, 'difference': s.difference}
                   for s in shifts],
    }
