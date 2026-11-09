"""
供應商與採購單：依低庫存原料產生建議採購單 (依供應商分單、補到目標庫存並進位到採購單位)，
編修 → 送出 (可 Email 給供應商) → 收貨 (入庫並建立批號、有效期限) → 完成。門市本機資料。
"""
import math
from datetime import date, datetime, timedelta

from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, PurchaseOrder, PurchaseOrderLine, Supplier
from app.services import inventory_service, settings_service

OPEN_STATUSES = ('draft', 'sent')
MAX_LINE_VALUE = 10_000_000   # 品項數量 / 單價上限：極大值 (例如 1e308) 相乘溢位，採購列表與採購單頁會 500
STATUS_LABELS = {'draft': ('草稿', 'secondary'), 'sent': ('已送出', 'info'), 'received': ('已收貨', 'success'),
                 'cancelled': ('已取消', 'dark')}


def _claim(po, allowed, message, **values):
    """條件式更新狀態 (目前狀態在 allowed 才改)：連按兩下 / 兩個分頁同時送出時只有一個會成功 (由呼叫端 commit)"""
    claimed = db.session.execute(update(PurchaseOrder).where(PurchaseOrder.id == po.id, PurchaseOrder.status.in_(allowed))
                                 .values(**values).execution_options(synchronize_session=False)).rowcount
    if not claimed:
        db.session.rollback()
        raise AppError(message, 409)
    db.session.refresh(po)


def suggest_qty(ing, pending=0):
    """建議採購量：補到目標庫存 (未設定時為安全量的 2 倍)，先扣掉已在未收貨採購單中的量，再進位到採購單位"""
    target = ing.par_level or (ing.low_threshold or 0) * 2
    need = target - (ing.stock or 0) - (pending or 0)
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
        qty = suggest_qty(ing, pending.get(ing.id, 0))
        if qty > 0:
            out.append((ing, qty))
    return out


def create_suggested_orders(tenant_id, staff):
    """依供應商建立建議採購單 (草稿)；回傳建立的採購單"""
    from app.services.db_locks import lock_tenant
    lock_tenant(tenant_id)   # 連按兩下 / 兩人同時按：依序處理 (PostgreSQL)，後到的看得到前一筆建立的草稿
    before = pending_qty()
    groups = {}
    for ing, qty in suggestions():
        # 預設供應商已停用：歸到「未指定」，不建立指向停用供應商的草稿 (送出時也不會寄給停用的供應商)
        sup_id = ing.supplier_id if (ing.supplier is not None and ing.supplier.is_active) else None
        groups.setdefault(sup_id, []).append((ing, qty))
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
    if created:   # 新增後再數一次 (SQLite 在 flush 時才取得寫入鎖)：同時有另一個請求建立了採購單就整筆取消
        mine = {}
        for po in created:
            for ln in po.lines:
                mine[ln.ingredient_id] = mine.get(ln.ingredient_id, 0) + ln.quantity
        after = pending_qty()
        if any(abs(after.get(i, 0) - q - before.get(i, 0)) > 1e-6 for i, q in mine.items()):
            db.session.rollback()
            raise AppError('另一個請求剛建立了採購單，請重新整理後確認', 409)
    return created


def _check_max(value, label):
    if value > MAX_LINE_VALUE:
        raise AppError(f'{label}過大 (上限 {MAX_LINE_VALUE:,})')


def update_lines(po, quantities, costs, add_ingredient_id=None, add_qty=0):
    """草稿編修：{line_id: 數量} (0 = 刪除)、{line_id: 單價}；可加入一項原料"""
    if po.status != 'draft':
        raise AppError('只有草稿可以修改')
    # 條件式 UPDATE 鎖住這張採購單並重新確認仍是草稿：與送出 / 收貨同時進行時，不會改到已送出或已收貨的品項
    _claim(po, ('draft',), '此採購單已送出或已收貨，無法修改', status='draft')
    for line in list(po.lines):
        if line.id in quantities:
            qty = max(0.0, float(quantities[line.id] or 0))
            if qty == 0:
                po.lines.remove(line)
                continue
            _check_max(qty, '數量')
            line.quantity = round(qty, 4)
        if line.id in costs and costs[line.id] is not None:
            _check_max(float(costs[line.id]), '單價')
            line.unit_cost = max(0.0, round(float(costs[line.id]), 4))
    if add_ingredient_id and add_qty and add_qty > 0:
        _check_max(add_qty, '數量')
        ing = db.session.get(Ingredient, add_ingredient_id)
        if ing is None or ing.tenant_id != po.tenant_id:
            raise AppError('查無此原料', 404)
        same = next((ln for ln in po.lines if ln.ingredient_id == ing.id), None)
        if same is not None:   # 已有這項原料：加到原本那一列 (同一原料兩列會讓收貨的加權平均成本難以對帳)
            _check_max((same.quantity or 0) + add_qty, '數量')
            same.quantity = round((same.quantity or 0) + add_qty, 4)
        else:
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
    _claim(po, ('draft',), '此採購單已送出', status='sent', sent_at=datetime.now())
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
    # 先搶到這張採購單才入庫：連按兩下收貨原本會入庫兩次 (庫存、批號、平均成本都加倍)
    _claim(po, OPEN_STATUSES, '此採購單已收貨或已取消', status='received', received_at=datetime.now(), received_by=staff)
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
        db.session.rollback()   # 退回上面搶到的「已收貨」
        raise AppError('請輸入實收數量')


def cancel(po):
    if po.status not in OPEN_STATUSES:
        raise AppError('此採購單已收貨或已取消')
    _claim(po, OPEN_STATUSES, '此採購單已收貨或已取消', status='cancelled')
