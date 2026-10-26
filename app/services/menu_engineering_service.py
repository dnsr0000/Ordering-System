"""
菜單工程分析 (Menu Engineering，Kasavana & Smith)：把每個餐點依「人氣 (銷售份數佔比)」與「每份貢獻毛利」分成四象限。

- 人氣門檻：平均佔比 × 70% (每個品項平均應賣出 1/N；達到 70% 以上算人氣高)
- 毛利門檻：全部品項的加權平均每份毛利 (總毛利 / 總份數)
- 明星 (高人氣、高毛利)：維持品質與曝光；金牛 (高人氣、低毛利)：檢討成本或小幅調價；
  潛力 (低人氣、高毛利)：加強推薦；瘦狗 (低人氣、低毛利)：考慮下架或重新設計。

成本：有配方用原料成本，沒有配方用餐點設定的單位成本；套餐另加賣出當下的配餐成本 (訂單明細的配餐快照)。
排除紅利兌換、促銷贈品、寄杯兌換等 0 元的明細；已取消、未付款的訂單不計。
"""
import io
import json
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from app.models.enums import OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order
from app.services.report_service import REVENUE_STATUSES, _unit_costs
from app.utils import neutralize_formulas

CLASSES = {
    'star': {'label': '明星', 'icon': '⭐', 'advice': '高人氣、高毛利：維持品質與曝光，可小幅調價'},
    'plowhorse': {'label': '金牛', 'icon': '🐂', 'advice': '高人氣、低毛利：檢討份量或原料成本、小幅調價，或搭配高毛利品做套餐'},
    'puzzle': {'label': '潛力', 'icon': '🧩', 'advice': '低人氣、高毛利：加強推薦 (點餐機推薦 / 待機輪播 / 套餐)、調整名稱或照片'},
    'dog': {'label': '瘦狗', 'icon': '🐕', 'advice': '低人氣、低毛利：考慮下架或重新設計 (必要品項可保留)'},
}
ORDER = ('star', 'plowhorse', 'puzzle', 'dog')


def classify(qty, unit_cm, pop_threshold, cm_threshold):
    high_pop, high_cm = qty >= pop_threshold, unit_cm >= cm_threshold
    if high_pop:
        return 'star' if high_cm else 'plowhorse'
    return 'puzzle' if high_cm else 'dog'


def analyze(tenant_id, start, end, node_uuid=None, category=''):
    """start / end 為 date (含兩端)"""
    t0 = datetime.combine(start, datetime.min.time())
    t1 = datetime.combine(end + timedelta(days=1), datetime.min.time())
    q = Order.query.filter(Order.tenant_id == tenant_id, Order.created_at >= t0, Order.created_at < t1,
                           Order.status != OrderStatus.CANCELLED)
    if node_uuid:
        q = q.filter(Order.node_uuid == node_uuid)
    orders = [o for o in q.all() if (o.payment_status or '') in REVENUE_STATUSES]
    costs = _unit_costs(tenant_id)
    menu = {m.id: m for m in MenuItem.query.filter_by(tenant_id=tenant_id).all()}
    by_uuid = {m.uuid: m.id for m in menu.values()}
    rows = {}
    for o in orders:
        for it in o.items:
            qty = it.quantity or 0
            if it.is_reward or it.is_gift or it.menu_item_id is None or qty <= 0 or (it.price or 0) <= 0:
                continue
            item = menu.get(it.menu_item_id)
            cat = ((item.category if item else '') or '未分類').strip()
            if category and cat != category:
                continue
            try:
                sides = json.loads(it.combo_sides) if it.combo_sides else []
            except ValueError:
                sides = []
            unit_cost = costs.get(it.menu_item_id, 0.0) + sum(costs.get(by_uuid.get(u), 0.0) for u in sides
                                                               if isinstance(u, str))
            row = rows.setdefault(it.menu_item_id, {'id': it.menu_item_id, 'name': item.name if item else it.item_name,
                                                    'category': cat, 'qty': 0, 'revenue': 0, 'cost': 0.0,
                                                    'no_cost': costs.get(it.menu_item_id, 0.0) <= 0})
            row['qty'] += qty
            row['revenue'] += (it.price or 0) * qty
            row['cost'] += unit_cost * qty
    items = list(rows.values())
    total_qty = sum(r['qty'] for r in items)
    total_cm = sum(r['revenue'] - r['cost'] for r in items)
    n = len(items)
    pop_threshold = (total_qty / n * 0.7) if n else 0
    cm_threshold = (total_cm / total_qty) if total_qty else 0
    for r in items:
        r['cost'] = round(r['cost'], 2)
        r['cm'] = round(r['revenue'] - r['cost'])
        r['unit_cm'] = round((r['revenue'] - r['cost']) / r['qty'], 1)
        r['avg_price'] = round(r['revenue'] / r['qty'], 1)
        r['food_cost_pct'] = round(r['cost'] / r['revenue'] * 100, 1) if r['revenue'] else 0
        r['mix_pct'] = round(r['qty'] / total_qty * 100, 1) if total_qty else 0
        r['cls'] = classify(r['qty'], r['unit_cm'], pop_threshold, cm_threshold)
        r['label'], r['icon'], r['advice'] = (CLASSES[r['cls']][k] for k in ('label', 'icon', 'advice'))
    items.sort(key=lambda r: (ORDER.index(r['cls']), -r['cm']))
    # 散佈圖座標 (0~100)：x = 銷售份數、y = 每份毛利
    max_qty = max((r['qty'] for r in items), default=0) or 1
    cms = [r['unit_cm'] for r in items] + [cm_threshold, 0]
    lo, hi = min(cms), max(cms)
    span = (hi - lo) or 1
    for r in items:
        r['x'] = round(r['qty'] / max_qty * 100, 2)
        r['y'] = round((r['unit_cm'] - lo) / span * 100, 2)
    return {
        'start': start.isoformat(), 'end': end.isoformat(), 'items': items, 'total_qty': total_qty,
        'total_cm': round(total_cm), 'pop_threshold': round(pop_threshold, 1), 'cm_threshold': round(cm_threshold, 1),
        'threshold_x': round(pop_threshold / max_qty * 100, 2), 'threshold_y': round((cm_threshold - lo) / span * 100, 2),
        'counts': {k: sum(1 for r in items if r['cls'] == k) for k in ORDER},
        'missing_cost': [r['name'] for r in items if r['no_cost']],
        'categories': sorted({((m.category or '未分類').strip()) for m in menu.values()}),
    }


def to_excel(result, shop_name=''):
    wb = Workbook()
    ws = wb.active
    ws.title = '菜單工程'
    ws.append(['店家', shop_name, '期間', f"{result['start']} ~ {result['end']}"])
    ws.append(['人氣門檻 (份)', result['pop_threshold'], '毛利門檻 (每份)', result['cm_threshold']])
    ws.append([])
    header = ['分類', '餐點', '象限', '份數', '佔比%', '平均售價', '每份成本', '每份毛利', '總毛利', '成本率%', '建議']
    ws.append(header)
    for c in ws[4]:
        c.font = Font(bold=True)
    for r in result['items']:
        ws.append([r['category'], r['name'], r['label'], r['qty'], r['mix_pct'], r['avg_price'],
                   round(r['cost'] / r['qty'], 1) if r['qty'] else 0, r['unit_cm'], r['cm'], r['food_cost_pct'], r['advice']])
    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
