"""
供應商與採購單：依低庫存原料產生建議採購單 (依供應商分單、補到目標庫存並進位到採購單位)，
編修 → 送出 (可 Email 給供應商) → 收貨 (入庫並建立批號、有效期限) → 完成。門市本機資料。
"""
import math
from datetime import date, datetime, timedelta

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, PurchaseOrder, PurchaseOrderLine, Supplier
from app.services import inventory_service, settings_service

OPEN_STATUSES = ('draft', 'sent')
STATUS_LABELS = {'draft': ('草稿', 'secondary'), 'sent': ('已送出', 'info'), 'received': ('已收貨', 'success'),
                 'cancelled': ('已取消', 'dark')}


def suggest_qty(ing):
    """建議採購量：補到目標庫存 (未設定時為安全量的 2 倍)，進位到採購單位"""
    target = ing.par_level or (ing.low_threshold or 0) * 2
    need = target - (ing.stock or 0)
    if need <= 0:
        return 0
    pack = ing.pack_size if (ing.pack_size or 0) > 0 else 1
    return round(math.ceil(round(need / pack, 6)) * pack, 4)


def pending_qty():
    """已在未收貨採購單中的數量 {ingredient_id: 數量}"""
    out = {}
    rows = db.session.query(PurchaseOrderLine.ingredient_id, PurchaseOrderLine.quantity) \
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id) \
        .filter(PurchaseOrder.status.in_(OPEN_STATUSES)).all()
    for ing_id, qty in rows:
        out[ing_id] = out.get(ing_id, 0) + (qty or 0)
    return out


def suggestions():
    """低於安全量、扣掉已下單數量後仍需補貨的原料 [(原料, 建議量)]"""
    pending = pending_qty()
    out = []
    for ing in Ingredient.query.filter_by(is_active=True).order_by(Ingredient.name).all():
        if not ing.is_low:
            continue
        qty = suggest_qty(ing)
        if pending.get(ing.id):
            qty = max(0, qty - pending[ing.id])
        if qty > 0:
            out.append((ing, qty))
    return out


def create_suggested_orders(tenant_id, staff):
    """依供應商建立建議採購單 (草稿)；回傳建立的採購單"""
    groups = {}
    for ing, qty in suggestions():
        groups.setdefault(ing.supplier_id, []).append((ing, qty))
    created = []
    for supplier_id, rows in groups.items():
        supplier = db.session.get(Supplier, supplier_id) if supplier_id else None
        po = PurchaseOrder(tenant_id=tenant_id, supplier_id=supplier_id, status='draft', created_by=staff,
                           created_at=datetime.now(),
                           expected_on=date.today() + timedelta(days=supplier.lead_days if supplier else 1),
                           note='系統依低庫存建議')
        po.lines = [PurchaseOrderLine(tenant_id=tenant_id, ingredient_id=ing.id, quantity=qty,
                                      unit_cost=round(ing.unit_cost or 0, 4)) for ing, qty in rows]
        db.session.add(po)
        created.append(po)
    db.session.flush()
    return created


def update_lines(po, quantities, costs, add_ingredient_id=None, add_qty=0):
    """草稿編修：{line_id: 數量} (0 = 刪除)、{line_id: 單價}；可加入一項原料"""
    if po.status != 'draft':
        raise AppError('只有草稿可以修改')
    for line in list(po.lines):
        if line.id in quantities:
            qty = max(0.0, float(quantities[line.id] or 0))
            if qty == 0:
                po.lines.remove(line)
                continue
            line.quantity = round(qty, 4)
        if line.id in costs and costs[line.id] is not None:
            line.unit_cost = max(0.0, round(float(costs[line.id]), 4))
    if add_ingredient_id and add_qty and add_qty > 0:
        ing = db.session.get(Ingredient, add_ingredient_id)
        if ing is None or ing.tenant_id != po.tenant_id:
            raise AppError('查無此原料', 404)
        po.lines.append(PurchaseOrderLine(tenant_id=po.tenant_id, ingredient_id=ing.id, quantity=round(add_qty, 4),
                                          unit_cost=round(ing.unit_cost or 0, 4)))


def po_text(po, shop_name=''):
    lines = [f'{shop_name} 採購單 {po.number}', f'預計到貨：{po.expected_on or "-"}', '']
    for ln in po.lines:
        ing = ln.ingredient
        lines.append(f'- {ing.name if ing else ln.ingredient_id}：{ln.quantity:g} {ing.unit if ing else ""}')
    if po.note:
        lines += ['', f'備註：{po.note}']
    return '\n'.join(lines)


def send(po, staff):
    """草稿 → 已送出；設定開啟且供應商有 Email 時寄出採購單"""
    from app.models.tenant import Tenant
    from app.services import deferred, mail_service
    if po.status != 'draft':
        raise AppError('只有草稿可以送出')
    if not po.lines:
        raise AppError('採購單沒有任何品項')
    po.status, po.sent_at = 'sent', datetime.now()
    emailed = False
    if settings_service.get('inventory', po.tenant_id).get('po_email_supplier') and po.supplier and po.supplier.email \
            and mail_service.is_configured(po.tenant_id):
        shop = db.session.get(Tenant, po.tenant_id)
        deferred.defer(mail_service.send_mail, po.tenant_id, [po.supplier.email], f'採購單 {po.number}',
                       po_text(po, shop.name if shop else ''))
        emailed = True
    return emailed


def receive(po, received, staff):
    """收貨：{line_id: (實收數量, 有效期限或 None)} → 入庫 (建立批號)；採購單改為已收貨"""
    if po.status not in OPEN_STATUSES:
        raise AppError('此採購單已收貨或已取消')
    any_in = False
    for line in po.lines:
        qty, expires_on = received.get(line.id, (line.quantity, None))
        qty = max(0.0, float(qty or 0))
        line.received_qty = round(qty, 4)
        if qty > 0 and line.ingredient is not None:
            inventory_service.adjust(line.ingredient, qty, 'purchase', note=f'採購單 {po.number}',
                                     unit_cost=line.unit_cost if line.unit_cost else None, expires_on=expires_on)
            any_in = True
    if not any_in:
        raise AppError('請輸入實收數量')
    po.status, po.received_at, po.received_by = 'received', datetime.now(), staff


def cancel(po):
    if po.status not in OPEN_STATUSES:
        raise AppError('此採購單已收貨或已取消')
    po.status = 'cancelled'
