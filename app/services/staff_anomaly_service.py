"""
員工異常偵測 (第三輪 #12)：依收銀員比較

- 作廢 / 退款：整單作廢 (Refund kind=cancel)、部分退款與單品作廢 (kind=partial) 的次數與金額，佔自己營業額的比例
- 折扣 / 改價：整單手動折扣 (order.manual_discount)、收銀改價 (order_item.original_price) 的次數與少收金額
- 交班帳差：關班時實點與應有現金的差額 (短少 / 溢收)

判斷：比例同時「高於全店平均的 N%」與「超過門檻 %」(或超過門檻且全店只有一位收銀員)，且次數達最少次數 → 標示異常；
交班短少累計超過帳差門檻也標示。每天可通知前一天的異常 (設定 → 員工異常偵測)。
資料：訂單、退款會同步到線上端，可在線上端看各分店；交班為門市本機資料 (只在門市看得到)。
"""
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.payment import Refund
from app.models.shift import CashShift
from app.models.user import StaffAuditLog
from app.services import settings_service

UNPAID = ('pending', 'expired', 'authorizing', 'unpaid')   # 尚未收款的訂單不計
FLAG_LABELS = {'void': '作廢 / 退款偏高', 'discount': '折扣 / 改價偏高', 'shift': '交班短少'}


def _blank():
    return {'sales_count': 0, 'sales': 0, 'cancel_count': 0, 'cancel_amount': 0, 'partial_count': 0,
            'partial_amount': 0, 'void_items': 0, 'discount_count': 0, 'discount_amount': 0, 'override_count': 0,
            'override_amount': 0, 'shifts': 0, 'shift_short': 0, 'shift_over': 0, 'short_count': 0}


def analyze(tenant_id, start, end, node_uuid=None):
    """start / end 為 date (含兩端)。回傳 {'staff': [...], 'store': {...}, 'cfg': {...}}"""
    cfg = settings_service.get('anomaly', tenant_id)
    t0 = datetime.combine(start, datetime.min.time())
    t1 = datetime.combine(end + timedelta(days=1), datetime.min.time())
    rows = defaultdict(_blank)
    order_q = Order.query.filter(Order.tenant_id == tenant_id, Order.created_at >= t0, Order.created_at < t1,
                                 Order.cashier_username != '', Order.cashier_username.is_not(None))
    if node_uuid:
        order_q = order_q.filter(Order.node_uuid == node_uuid)
    for o in order_q.all():
        if (o.payment_status or '') in UNPAID:
            continue
        r = rows[o.cashier_username]
        r['sales_count'] += 1
        r['sales'] += o.total_price or 0     # 含之後被作廢 / 退款的訂單 (作廢率的分母)
        if (o.manual_discount or 0) > 0 and o.status != OrderStatus.CANCELLED:
            r['discount_count'] += 1
            r['discount_amount'] += o.manual_discount
    # 收銀改價 (改價前後的差額 × 份數)
    over_q = db.session.query(Order.cashier_username, func.count(OrderItem.id),
                              func.sum((OrderItem.original_price - OrderItem.price) * OrderItem.quantity)) \
        .join(Order, Order.id == OrderItem.order_id) \
        .filter(Order.tenant_id == tenant_id, Order.created_at >= t0, Order.created_at < t1,
                OrderItem.original_price.is_not(None), Order.cashier_username != '')
    if node_uuid:
        over_q = over_q.filter(Order.node_uuid == node_uuid)
    for name, cnt, amount in over_q.group_by(Order.cashier_username).all():
        rows[name]['override_count'] += cnt
        rows[name]['override_amount'] += max(0, int(amount or 0))
    # 作廢 / 退款 (以執行的員工為準)
    ref_q = Refund.query.filter(Refund.tenant_id == tenant_id, Refund.created_at >= t0, Refund.created_at < t1)
    if node_uuid:
        ref_q = ref_q.join(Order, Order.id == Refund.order_id).filter(Order.node_uuid == node_uuid)
    for ref in ref_q.all():
        name = ref.staff_username or 'system'
        if name == 'system':
            continue
        key = 'cancel' if ref.kind == 'cancel' else 'partial'
        rows[name][f'{key}_count'] += 1
        rows[name][f'{key}_amount'] += ref.amount or 0
    for name, cnt in db.session.query(StaffAuditLog.staff_username, func.count(StaffAuditLog.id)) \
            .filter(StaffAuditLog.tenant_id == tenant_id, StaffAuditLog.action_type == 'VOID_ITEM',
                    StaffAuditLog.created_at >= t0, StaffAuditLog.created_at < t1) \
            .group_by(StaffAuditLog.staff_username).all():
        if name and name != 'system':
            rows[name]['void_items'] += cnt
    if not node_uuid:
        for s in CashShift.query.filter(CashShift.tenant_id == tenant_id, CashShift.status == 'closed',
                                        CashShift.closed_at >= t0, CashShift.closed_at < t1).all():
            r = rows[s.closed_by or 'system']
            r['shifts'] += 1
            if (s.difference or 0) < 0:
                r['shift_short'] += -s.difference
                r['short_count'] += 1
            elif (s.difference or 0) > 0:
                r['shift_over'] += s.difference
    rows.pop('system', None)

    total = _blank()
    for r in rows.values():
        for k, v in r.items():
            total[k] += v
    store_void = _rate(total['cancel_amount'] + total['partial_amount'], total['sales'])
    store_disc = _rate(total['discount_amount'] + total['override_amount'], total['sales'])
    multiple = (cfg.get('rate_multiple') or 200) / 100
    min_events = int(cfg.get('min_events') or 3)
    single = len([r for r in rows.values() if r['sales_count']]) <= 1
    out = []
    for name, r in rows.items():
        void_events = r['cancel_count'] + r['partial_count']
        disc_events = r['discount_count'] + r['override_count']
        r['void_rate'] = _rate(r['cancel_amount'] + r['partial_amount'], r['sales'])
        r['discount_rate'] = _rate(r['discount_amount'] + r['override_amount'], r['sales'])
        flags = []
        if void_events >= min_events and r['void_rate'] >= (cfg.get('void_rate_limit') or 5) \
                and (single or r['void_rate'] >= store_void * multiple):
            flags.append('void')
        if disc_events >= min_events and r['discount_rate'] >= (cfg.get('discount_rate_limit') or 10) \
                and (single or r['discount_rate'] >= store_disc * multiple):
            flags.append('discount')
        limit = int(cfg.get('shift_diff_limit') or 0)
        if limit and r['shift_short'] >= limit:
            flags.append('shift')
        r.update(name=name, flags=flags, flag_labels=[FLAG_LABELS[f] for f in flags],
                 score=len(flags) * 100 + r['void_rate'] + r['discount_rate'])
        out.append(r)
    out.sort(key=lambda r: (-r['score'], r['name']))
    total.update(void_rate=store_void, discount_rate=store_disc)
    return {'staff': out, 'store': total, 'cfg': cfg, 'start': start.isoformat(), 'end': end.isoformat()}


def _rate(amount, base):
    return round(amount / base * 100, 1) if base else (100.0 if amount else 0.0)


def notify_daily(tenant_id, today):
    """每天通知前一天的異常；回傳被標示的員工數"""
    from app.services import monitor_service
    if not settings_service.get('anomaly', tenant_id).get('notify'):
        return 0
    day = today - timedelta(days=1)
    result = analyze(tenant_id, day, day)
    flagged = [r for r in result['staff'] if r['flags']]
    if flagged:
        detail = '；'.join(f"{r['name']}：{'、'.join(r['flag_labels'])} (作廢 {r['void_rate']}%、折扣 {r['discount_rate']}%、"
                          f"交班短少 ${r['shift_short']})" for r in flagged)
        monitor_service.alert(f'{day:%m/%d} 員工異常：{detail}', key=f'anomaly-{tenant_id}-{day}', tenant_id=tenant_id,
                              level='warning', source='anomaly')
    return len(flagged)
