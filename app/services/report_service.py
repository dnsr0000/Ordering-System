"""
進階營運報表：營業額、毛利 (配方成本或餐點單位成本)、時段熱度、付款方式、管道、分類與品項排行、
分店比較 (線上端)、收銀員、促銷成效；可匯出 Excel 並定期寄送。
"""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.enums import PAYMENT_METHOD_LABELS, OrderStatus
from app.models.inventory import RecipeLine
from app.models.menu import WEEKDAY_LABELS, MenuItem
from app.models.order import Order
from app.models.payment import PaymentRecord, Refund
from app.models.promotion import Promotion
from app.models.sync import SyncNode
from app.services import accounting_service, settings_service
from app.sync import runtime as sync_runtime
from app.utils import XLSX_MIME, day_bounds, workbook_bytes

REVENUE_STATUSES = ('', 'settled', 'paid')
SOURCE_LABELS = {'Kiosk': '點餐機', 'POS': 'POS', 'Online': '線上點餐', 'Table': '桌邊掃碼', 'UberEats': 'Uber Eats',
                 'foodpanda': 'foodpanda', 'Delivery': '外送 (手動)'}
CATCHUP_DAYS = 7      # 每天寄送的報表漏寄時，最多往回補幾天 (合併成一封)
MAX_SHIFT_HOURS = 16  # 單筆打卡超過這個時數視為忘了打下班卡 (未下班的不計入人事成本，已下班的列出提醒店長確認)


def unit_costs(tenant_id):
    """{menu_item_id: 每份成本}：有配方用原料成本，否則用餐點設定的單位成本"""
    costs = {m.id: float(m.unit_cost or 0) for m in MenuItem.query.filter_by(tenant_id=tenant_id).all()}
    recipe = defaultdict(float)
    for rl in RecipeLine.query.filter_by(tenant_id=tenant_id).all():
        if rl.ingredient is not None:
            recipe[rl.menu_item_id] += (rl.quantity or 0) * (rl.ingredient.unit_cost or 0)
    costs.update(recipe)
    return costs


def is_revenue(order):
    """計入營業額的訂單：未取消且已收款 (或免付款)"""
    return order.status != OrderStatus.CANCELLED and (order.payment_status or '') in REVENUE_STATUSES


def _bounds(start, end):
    """date (含兩端) 或 datetime (半開區間) 都可"""
    return (start, end) if isinstance(start, datetime) else day_bounds(start, end)


def orders_between(tenant_id, start, end, node_uuid=None):
    """期間內 (依建立時間) 的所有訂單，含已取消；start / end 為 date 或 datetime。
    預載明細：報表、即時看板、菜單工程都會逐張讀 o.items (lazy)，不預載會變成每張訂單一次查詢 (N+1)"""
    t0, t1 = _bounds(start, end)
    q = Order.query.options(selectinload(Order.items)).filter(
        Order.tenant_id == tenant_id, Order.created_at >= t0, Order.created_at < t1)
    if node_uuid:
        q = q.filter(Order.node_uuid == node_uuid)
    return q.all()


def revenue_orders(tenant_id, start, end, node_uuid=None):
    """期間內計入營業額的訂單 (見 is_revenue)"""
    return [o for o in orders_between(tenant_id, start, end, node_uuid) if is_revenue(o)]


def _sides_cost(item, by_uuid, costs):
    """套餐配餐 (OrderItem.combo_sides 的 uuid 清單) 的單位成本合計"""
    if not item.combo_sides:
        return 0.0
    try:
        sides = json.loads(item.combo_sides)
    except ValueError:
        return 0.0
    return sum(costs.get(by_uuid.get(u), 0.0) for u in sides if isinstance(u, str))


def _add_promotions(promotions, order, gift_promos):
    """
    促銷成效：訂單上每個成立的活動都計一次 (滿額贈折扣為 0 也要計次)；
    促銷折扣只分給折扣類活動 (滿額贈不分)，整除不盡的餘數給前面的活動，合計等於訂單的促銷折扣。
    訂單只存活動名稱與折扣合計，多個折扣活動之間仍是平均分攤。
    """
    names = [n for n in order.promotion_names.split('、') if n]
    gift_lines = {it.customization for it in order.items if it.is_gift}
    discount_names = [n for n in names if n not in gift_promos and f'[{n}] 贈品' not in gift_lines] or names
    total = order.promotion_discount or 0
    share, extra = divmod(total, len(discount_names)) if discount_names else (0, 0)
    for name in names:
        promotions[name]['count'] += 1
    for i, name in enumerate(discount_names):
        promotions[name]['amount'] += share + (1 if i < extra else 0)


def avg_ticket(revenue, orders):
    """客單價：分母只算實付大於 0 的訂單 (純點數兌換、寄杯扣次的 0 元單不算)，與後台總覽一致"""
    paid = sum(1 for o in orders if (o.total_price or 0) > 0)
    return round(revenue / paid) if paid else 0


def build_report(tenant_id, start, end, node_uuid=None):
    """start / end 為 date (含兩端)"""
    t0, t1 = day_bounds(start, end)
    all_orders = orders_between(tenant_id, start, end, node_uuid)
    orders = [o for o in all_orders if is_revenue(o)]
    costs = unit_costs(tenant_id)
    menu = {m.id: m for m in MenuItem.query.filter_by(tenant_id=tenant_id).all()}

    gross = sum(o.subtotal for o in orders)
    net = sum(o.total_price or 0 for o in orders)
    # 部分退款只扣「仍計入營業額」的訂單：之後整張取消的訂單已整筆不算營業額，再扣它的部分退款會重複扣
    refunds_q = Refund.query.join(Order, Order.id == Refund.order_id).filter(
        Refund.tenant_id == tenant_id, Refund.created_at >= t0, Refund.created_at < t1, Refund.kind == 'partial',
        Order.status != OrderStatus.CANCELLED)
    if node_uuid:   # 依分店：只算該分店訂單的退款
        refunds_q = refunds_q.filter(Order.node_uuid == node_uuid)
    partial_refunds = sum(r.amount for r in refunds_q.all())
    by_uuid = {m.uuid: m.id for m in menu.values()}
    cogs = 0.0
    items = {}
    categories = defaultdict(lambda: {'qty': 0, 'revenue': 0, 'cost': 0.0})
    for o in orders:
        for it in o.items:
            # 套餐的配餐不是獨立的明細，只記在主餐的 combo_sides (uuid)：成本要一起算 (與菜單工程、扣庫存相同)
            cost = (costs.get(it.menu_item_id, 0.0) + _sides_cost(it, by_uuid, costs)) * (it.quantity or 0)
            cogs += cost
            key = it.item_name
            row = items.setdefault(key, {'name': key, 'qty': 0, 'revenue': 0, 'cost': 0.0})
            row['qty'] += it.quantity or 0
            row['revenue'] += (it.price or 0) * (it.quantity or 0)
            row['cost'] += cost
            cat = (menu[it.menu_item_id].category if it.menu_item_id in menu else '其他') or '未分類'
            categories[cat]['qty'] += it.quantity or 0
            categories[cat]['revenue'] += (it.price or 0) * (it.quantity or 0)
            categories[cat]['cost'] += cost
    for row in items.values():
        row['margin'] = row['revenue'] - row['cost']
        row['margin_rate'] = round(row['margin'] / row['revenue'] * 100, 1) if row['revenue'] else 0
    by_day = defaultdict(lambda: {'count': 0, 'amount': 0})
    heat = [[0] * 24 for _ in range(7)]
    heat_amount = [[0] * 24 for _ in range(7)]
    by_source = defaultdict(lambda: {'count': 0, 'amount': 0})
    by_cashier = defaultdict(lambda: {'count': 0, 'amount': 0})
    by_node = defaultdict(lambda: {'count': 0, 'amount': 0})
    promotions = defaultdict(lambda: {'count': 0, 'amount': 0})
    gift_promos = {name for (name,) in db.session.query(Promotion.name).filter(
        Promotion.tenant_id == tenant_id, Promotion.kind == 'threshold_gift')}
    for o in orders:
        d = o.created_at.date().isoformat()
        by_day[d]['count'] += 1
        by_day[d]['amount'] += o.total_price or 0
        wd, hr = o.created_at.weekday(), o.created_at.hour
        heat[wd][hr] += 1
        heat_amount[wd][hr] += o.total_price or 0
        src = SOURCE_LABELS.get(o.order_source or 'Kiosk', o.order_source or 'Kiosk')
        by_source[src]['count'] += 1
        by_source[src]['amount'] += o.total_price or 0
        if o.cashier_username:
            by_cashier[o.cashier_username]['count'] += 1
            by_cashier[o.cashier_username]['amount'] += o.total_price or 0
        by_node[o.node_uuid or '本機']['count'] += 1
        by_node[o.node_uuid or '本機']['amount'] += o.total_price or 0
        if o.promotion_names:
            _add_promotions(promotions, o, gift_promos)
    pay_q = PaymentRecord.query.filter(PaymentRecord.tenant_id == tenant_id, PaymentRecord.created_at >= t0,
                                       PaymentRecord.created_at < t1)
    if node_uuid:   # 依分店：有訂單的以訂單的分店為準，沒有訂單的收款 (桌位結帳、儲值等) 依收款紀錄自己的 node_uuid (與會計傳票、損益表相同)
        pay_q = accounting_service.filter_payments_by_node(pay_q, node_uuid)
    by_method = defaultdict(lambda: {'in': 0, 'out': 0, 'net': 0})
    for p in pay_q.all():
        # 零用金不是營業收款；訂金沒收只是預收轉收入 (錢在收訂金時已計入)，不是金流，計入會重複算一次收入
        if p.kind in ('pay_in', 'pay_out', 'deposit_forfeit'):
            continue
        label = PAYMENT_METHOD_LABELS.get(p.method, p.method)
        if p.amount >= 0:
            by_method[label]['in'] += p.amount
        else:
            by_method[label]['out'] += -p.amount
        by_method[label]['net'] += p.amount
    days = (end - start).days + 1
    report = {
        'start': start.isoformat(), 'end': end.isoformat(), 'days': days,
        'order_count': len(orders), 'cancelled_count': sum(1 for o in all_orders if o.status == OrderStatus.CANCELLED),
        'gross': gross, 'discounts': sum(o.discount_amount or 0 for o in orders),
        'service_charge': sum(o.service_charge or 0 for o in orders), 'net': net,
        'partial_refunds': partial_refunds, 'net_after_refunds': net - partial_refunds,
        'cogs': round(cogs), 'gross_margin': round(net - partial_refunds - cogs),
        'margin_rate': round((net - partial_refunds - cogs) / net * 100, 1) if net else 0,
        'avg_ticket': avg_ticket(net, orders),
        'per_day': round(net / days) if days else 0,
        'by_day': [dict(day=k, **v) for k, v in sorted(by_day.items())],
        'heat': heat, 'heat_amount': heat_amount, 'heat_max': max((max(r) for r in heat), default=0),
        'by_source': dict(by_source), 'by_method': dict(by_method), 'by_cashier': dict(by_cashier),
        'by_node': dict(by_node), 'promotions': dict(promotions),
        'categories': {k: dict(v, cost=round(v['cost']), margin=round(v['revenue'] - v['cost'])) for k, v in categories.items()},
        'items': sorted(items.values(), key=lambda r: -r['revenue']),
    }
    report['day_max'] = max((d['amount'] for d in report['by_day']), default=0)
    # 分店名稱 (Excel 匯出、定期寄送用；查不到的分店顯示 uuid)
    node_keys = [k for k in by_node if k != '本機']
    report['node_names'] = {n.uuid: n.name for n in SyncNode.query.filter(
        SyncNode.tenant_id == tenant_id, SyncNode.uuid.in_(node_keys))} if node_keys else {}
    # 打卡紀錄只在各門市主機 (不同步)：線上端或依分店檢視時不計算人事成本 (與損益表相同)
    report['labor_available'] = not node_uuid and not sync_runtime.is_hub()
    report.update(labor_cost(tenant_id, t0, t1, report['net_after_refunds']) if report['labor_available'] else
                  {'labor_hours': 0, 'labor_cost': 0, 'labor_ratio': 0, 'labor_by_staff': [], 'labor_missing_wage': [],
                   'labor_review': []})
    return report


def labor_cost(tenant_id, t0, t1, revenue):
    """
    人事成本：期間內的打卡工時 × 員工時薪 (員工帳號頁設定)，與營業額相比的人事成本率。
    尚未下班的紀錄：上班不到 MAX_SHIFT_HOURS 的 (今天還在班) 以現在時間計算；更久的視為忘了打下班卡，不計入工時，
    與超過 MAX_SHIFT_HOURS 的已下班紀錄 (例如隔天才補打下班卡) 一起列在 labor_review 提醒店長修正
    (counted=False 表示未計入)；沒有設定時薪的員工列在 labor_missing_wage 提醒補填。
    """
    from app.models.staff import TimeClock
    now = datetime.now()
    by_staff = {}
    review = []
    for r in TimeClock.query.filter(TimeClock.tenant_id == tenant_id, TimeClock.clock_in >= t0, TimeClock.clock_in < t1) \
            .execution_options(skip_tenant_filter=True).order_by(TimeClock.clock_in).all():
        name = r.staff.username if r.staff else str(r.staff_account_id)
        open_too_long = r.clock_out is None and now - r.clock_in > timedelta(hours=MAX_SHIFT_HOURS)
        end = r.clock_out or min(now, t1)
        hours = 0.0 if open_too_long else max(0.0, (end - r.clock_in).total_seconds() / 3600)
        if open_too_long or hours > MAX_SHIFT_HOURS:
            review.append({'id': r.id, 'name': name, 'clock_in': r.clock_in, 'clock_out': r.clock_out,
                           'hours': round(hours, 2), 'counted': not open_too_long})
        row = by_staff.setdefault(r.staff_account_id, {'name': name,
                                                       'wage': (r.staff.hourly_wage or 0) if r.staff else 0, 'hours': 0.0})
        row['hours'] += hours
    rows = [dict(v, hours=round(v['hours'], 2), cost=round(v['hours'] * v['wage'])) for v in by_staff.values()]
    rows.sort(key=lambda r: -r['cost'])
    total = sum(r['cost'] for r in rows)
    return {'labor_hours': round(sum(r['hours'] for r in rows), 2), 'labor_cost': total,
            'labor_ratio': round(total / revenue * 100, 1) if revenue else 0, 'labor_by_staff': rows,
            'labor_missing_wage': [r['name'] for r in rows if not r['wage'] and r['hours'] > 0], 'labor_review': review}


def report_to_excel(report, shop_name=''):
    wb = Workbook()
    ws = wb.active
    ws.title = '總覽'
    rows = [('店家', shop_name), ('期間', f"{report['start']} ~ {report['end']}"), ('訂單數', report['order_count']),
            ('取消單數', report['cancelled_count']), ('商品小計', report['gross']), ('折扣', report['discounts']),
            ('服務費', report['service_charge']), ('營業額', report['net']), ('部分退款', report['partial_refunds']),
            ('退款後營業額', report['net_after_refunds']), ('估計成本', report['cogs']), ('毛利', report['gross_margin']),
            ('毛利率 %', report['margin_rate']), ('客單價', report['avg_ticket']), ('日均營業額', report['per_day'])]
    for r in rows:
        ws.append(r)
    for cell in ws['A']:
        cell.font = Font(bold=True)

    def sheet(title, header, data):
        s = wb.create_sheet(title)
        s.append(header)
        for c in s[1]:
            c.font = Font(bold=True)
        for row in data:
            s.append(row)

    sheet('每日', ['日期', '訂單數', '營業額'], [(d['day'], d['count'], d['amount']) for d in report['by_day']])
    sheet('品項', ['品項', '數量', '營收', '成本', '毛利', '毛利率%'],
          [(i['name'], i['qty'], i['revenue'], round(i['cost']), round(i['margin']), i['margin_rate']) for i in report['items']])
    sheet('分類', ['分類', '數量', '營收', '成本', '毛利'],
          [(k, v['qty'], v['revenue'], v['cost'], v['margin']) for k, v in report['categories'].items()])
    sheet('付款方式', ['方式', '收款', '退款', '淨額'], [(k, v['in'], v['out'], v['net']) for k, v in report['by_method'].items()])
    sheet('點餐管道', ['管道', '訂單數', '營業額'], [(k, v['count'], v['amount']) for k, v in report['by_source'].items()])
    sheet('時段熱度', ['星期'] + [f'{h}時' for h in range(24)],
          [[f'週{WEEKDAY_LABELS[i]}'] + report['heat'][i] for i in range(7)])
    sheet('收銀員', ['帳號', '訂單數', '營業額'], [(k, v['count'], v['amount']) for k, v in report['by_cashier'].items()])
    if len(report['by_node']) > 1:
        names = report.get('node_names') or {}
        sheet('分店', ['分店', '訂單數', '營業額'],
              [(names.get(k, k), v['count'], v['amount']) for k, v in report['by_node'].items()])
    return workbook_bytes(wb)


def period_for(frequency, today=None):
    today = today or date.today()
    if frequency == 'weekly':
        end = today - timedelta(days=today.weekday() + 1)      # 上週日
        return end - timedelta(days=6), end
    y = today - timedelta(days=1)
    return y, y


def send_scheduled_report(tenant_id, today=None, force=False):
    """
    依「定期寄送報表」設定寄出最近一期 (每週：上週一~週日；每天：昨天)；回傳寄出的收件人數。
    排程 (force=False) 記錄最後寄出的期間：最近一期還沒寄就補寄 (例如週一公休沒開機，週二開機時補寄上週報表)；
    每天頻率漏寄的日子 (最多 CATCHUP_DAYS 天) 合併成一封。
    分店使用總部下發的設定時不由排程寄出 (由線上端寄全部分店的報表；分店沒有 SMTP 密碼)，手動「立即寄送」不受限。
    """
    from app.models.tenant import Tenant
    from app.services import mail_service
    cfg = settings_service.get('report_email', tenant_id)
    if not cfg.get('enabled') and not force:
        return 0
    today = today or date.today()
    frequency = cfg.get('frequency') or 'daily'
    start, end = period_for(frequency, today)
    marker = f'report_sent:{tenant_id}:{frequency}'
    if not force:
        if settings_service.source('report_email', tenant_id) == 'hq':
            return 0
        last = _parse_day(sync_runtime.get_state(marker, ''))
        if last is not None and last >= end:
            return 0
        if frequency != 'weekly' and last is not None:
            start = max(last + timedelta(days=1), end - timedelta(days=CATCHUP_DAYS - 1))
    shop = db.session.get(Tenant, tenant_id)
    report = build_report(tenant_id, start, end)
    body = (f"{shop.name if shop else ''} 營運報表 {start} ~ {end}\n\n"
            f"營業額：${report['net']:,}\n訂單數：{report['order_count']}\n客單價：${report['avg_ticket']:,}\n"
            f"毛利 (估計)：${report['gross_margin']:,} ({report['margin_rate']}%)\n\n詳細內容請見附件 Excel。")
    xlsx = report_to_excel(report, shop.name if shop else '').getvalue()
    sent = mail_service.send_mail(tenant_id, cfg.get('recipients') or '', f'營運報表 {start} ~ {end}', body,
                                  attachments=[(f'report_{start}_{end}.xlsx', xlsx, XLSX_MIME)])
    if sent and not force:
        sync_runtime.set_state(marker, end.isoformat())
        db.session.commit()
    return sent


def _parse_day(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
