"""
原物料庫存：依配方扣料、取消回補、進貨、盤點、低庫存警示。

- 結帳時依「餐點 (含套餐配餐) × 每份用量」扣料，與訂單同一交易；設定「原料不足時禁止販售」時以條件式 UPDATE 扣料，
  不足則整筆結帳失敗。
- 取消訂單時依該訂單的扣料紀錄回補。
- 低於安全量時通知 (每種原料回到安全量以上之前只通知一次)。
- 批號效期：每次進貨建立一個批號 (可填有效期限)；扣料、報廢、盤虧依「先到期先出」扣減各批剩餘量，
  即將到期 / 已過期的批號在原料頁顯示並通知 (原料總量仍以 ingredient.stock 為準)。
"""
import re
from datetime import date, datetime, timedelta

from sqlalchemy import Numeric, cast, func, select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, IngredientLot, RecipeLine, StockMovement
from app.services import deferred, settings_service
from app.utils import current_staff_name as _staff
from app.utils import safe_int


enabled = settings_service.flag('inventory')

EPS = 1e-6   # 數量比較容差 (庫存為浮點數，小數用量累減會有 0.09999999999999848 這類誤差)
_LOT_EXP_NOTE = '批號效期 '   # 扣料異動備註：記下扣用批號中最早的有效期限，取消回補時沿用
_LOT_EXP_RE = re.compile(_LOT_EXP_NOTE + r'(\d{4}-\d{2}-\d{2})')


def _stock_plus(delta):
    """SQL：stock + delta 並四捨五入到 4 位，避免 double 累加誤差 (PostgreSQL 的 round 只接受 numeric，先轉型)"""
    return func.round(cast(Ingredient.stock + delta, Numeric), 4)


def _stock_now(ingredient_id):
    """資料庫中目前的庫存 (本交易內已用 UPDATE 改過的值)"""
    return db.session.execute(select(Ingredient.stock).where(Ingredient.id == ingredient_id)
                              .execution_options(skip_tenant_filter=True)).scalar() or 0


def needs_for(demands):
    """{menu_item_id: 份數} → {ingredient_id: 用量}"""
    if not demands:
        return {}
    needs = {}
    for rl in RecipeLine.query.filter(RecipeLine.menu_item_id.in_(list(demands))).all():
        needs[rl.ingredient_id] = needs.get(rl.ingredient_id, 0.0) + (rl.quantity or 0) * demands[rl.menu_item_id]
    return {k: round(v, 4) for k, v in needs.items() if v}


def consume(tenant_id, order_id, demands, block=True):
    """結帳扣料 (由結帳交易呼叫)；原料不足且設定為禁止販售時拋 AppError。
    block=False：訂單已在別處成立 (分店收到線上訂單)，原料不足也照扣 (庫存可為負數)，不拋 AppError"""
    if not enabled(tenant_id):
        return {}
    cfg = settings_service.get('inventory', tenant_id)
    needs = needs_for(demands)
    for ing_id in sorted(needs):
        qty = needs[ing_id]
        stmt = update(Ingredient).where(Ingredient.id == ing_id, Ingredient.tenant_id == tenant_id)
        if block and cfg.get('block_when_short'):
            stmt = stmt.where(Ingredient.stock >= qty - EPS)
        if db.session.execute(stmt.values(stock=_stock_plus(-qty)).execution_options(synchronize_session=False)).rowcount != 1:
            if not block:
                continue   # 原料已不存在：略過 (不阻擋已成立的訂單)
            ing = db.session.get(Ingredient, ing_id)
            raise AppError(f'原料【{ing.name if ing else ing_id}】庫存不足，暫停供應相關餐點！', insufficient_stock=True)
        expires_on = _draw(ing_id, qty)[1]
        db.session.add(StockMovement(tenant_id=tenant_id, ingredient_id=ing_id, delta=-qty, reason='consume',
                                     order_id=order_id, staff_username=_staff(),
                                     note=f'{_LOT_EXP_NOTE}{expires_on.isoformat()}' if expires_on else ''))
    if needs:
        deferred.defer(check_low_stock, tenant_id, sorted(needs))
    return needs


def _order_movements(order_id):
    """訂單的扣料 / 回補紀錄 → ({原料: 淨扣量 (負數)}, {原料: 扣用批號中最早的有效期限})"""
    net, expiry = {}, {}
    for mv in StockMovement.query.filter(StockMovement.order_id == order_id,
                                         StockMovement.reason.in_(('consume', 'restore'))).all():
        net[mv.ingredient_id] = round(net.get(mv.ingredient_id, 0) + (mv.delta or 0), 4)
        m = _LOT_EXP_RE.search(mv.note or '') if mv.reason == 'consume' else None
        if m:
            exp = date.fromisoformat(m.group(1))
            expiry[mv.ingredient_id] = min(exp, expiry.get(mv.ingredient_id, exp))
    return net, expiry


def restore(tenant_id, order_id):
    """取消訂單：依扣料紀錄回補 (扣掉先前單品作廢已回補的量，不重複回補)；回補批號沿用扣用批號的有效期限"""
    net, expiry = _order_movements(order_id)
    rows = 0
    for ing_id, used in sorted(net.items()):
        if used >= 0:
            continue
        db.session.execute(update(Ingredient).where(Ingredient.id == ing_id)
                           .values(stock=_stock_plus(-used)).execution_options(synchronize_session=False))
        db.session.add(StockMovement(tenant_id=tenant_id, ingredient_id=ing_id, delta=-used, reason='restore',
                                     order_id=order_id, staff_username=_staff(), note=f'取消訂單 #{order_id} 回補'))
        _add_lot(tenant_id, ing_id, -used, expires_on=expiry.get(ing_id), source='restore', note=f'取消訂單 #{order_id}',
                 stock_after=_stock_now(ing_id))
        rows += 1
    return rows


def restore_items(tenant_id, order_id, demands, note=''):
    """單品作廢：依配方回補這幾份餐點用掉的原料 (只在有扣料紀錄的訂單上回補，最多回補到該訂單扣過的量)"""
    if not demands:
        return {}
    net, expiry = _order_movements(order_id)
    back = {}
    for ing_id, qty in needs_for(demands).items():
        qty = min(qty, -net.get(ing_id, 0))
        if qty <= 0:
            continue
        db.session.execute(update(Ingredient).where(Ingredient.id == ing_id)
                           .values(stock=_stock_plus(qty)).execution_options(synchronize_session=False))
        db.session.add(StockMovement(tenant_id=tenant_id, ingredient_id=ing_id, delta=qty, reason='restore',
                                     order_id=order_id, staff_username=_staff(), note=note[:200]))
        _add_lot(tenant_id, ing_id, qty, expires_on=expiry.get(ing_id), source='restore', note=note[:100],
                 stock_after=_stock_now(ing_id))
        back[ing_id] = qty
    return back


def adjust(ingredient, delta, reason, note='', unit_cost=None, expires_on=None):
    """進貨 (purchase，建立批號，可填有效期限)、報廢 (waste)、手動調整 (adjust)；進貨可更新單位成本 (加權平均)"""
    delta = round(float(delta or 0), 4)
    if delta == 0:
        raise AppError('數量不可為 0')
    if reason == 'purchase' and delta < 0:
        raise AppError('進貨數量必須大於 0')
    db.session.execute(update(Ingredient).where(Ingredient.id == ingredient.id)
                       .values(stock=_stock_plus(delta)).execution_options(synchronize_session=False))
    # 以資料庫中的庫存為準 (同一交易內先前以 UPDATE 改過的量，例如同一張採購單有兩列相同原料)，並同步回物件
    stock_after = _stock_now(ingredient.id)
    set_committed_value(ingredient, 'stock', stock_after)
    if reason in ('purchase', 'transfer_in') and unit_cost is not None and unit_cost >= 0:
        old_qty = max(0.0, stock_after - delta)
        total = old_qty + delta
        ingredient.unit_cost = round(((ingredient.unit_cost or 0) * old_qty + unit_cost * delta) / total, 4) if total else unit_cost
    db.session.add(StockMovement(tenant_id=ingredient.tenant_id, ingredient_id=ingredient.id, delta=delta, reason=reason,
                                 unit_cost=unit_cost or 0, note=str(note or '')[:200], staff_username=_staff()))
    if delta > 0:
        _add_lot(ingredient.tenant_id, ingredient.id, delta, expires_on=expires_on, unit_cost=unit_cost or 0,
                 source='purchase' if reason == 'purchase' else reason, note=note, stock_after=stock_after)
    else:
        draw_lots(ingredient.id, -delta)
    deferred.defer(check_low_stock, ingredient.tenant_id, [ingredient.id])


def record_movement(ingredient, delta, reason, note='', unit_cost=0):
    """庫存已由呼叫端以條件式 UPDATE 增減 (例如叫貨 / 調撥出貨)：補寫異動紀錄、批號 (先到期先出) 與低庫存檢查"""
    delta = round(float(delta or 0), 4)
    db.session.add(StockMovement(tenant_id=ingredient.tenant_id, ingredient_id=ingredient.id, delta=delta, reason=reason,
                                 unit_cost=unit_cost or 0, note=str(note or '')[:200], staff_username=_staff()))
    if delta < 0:
        draw_lots(ingredient.id, -delta)
    elif delta > 0:
        _add_lot(ingredient.tenant_id, ingredient.id, delta, unit_cost=unit_cost or 0, source=reason, note=note,
                 stock_after=_stock_now(ingredient.id))
    deferred.defer(check_low_stock, ingredient.tenant_id, [ingredient.id])


def stocktake(ingredient, counted, note=''):
    """盤點：以實際數量覆蓋，差額記為 stocktake 異動"""
    counted = round(float(counted), 4)
    if counted < 0:
        raise AppError(f'原料【{ingredient.name}】的盤點數量不可為負數')
    db.session.refresh(ingredient, ['stock'])
    delta = round(counted - (ingredient.stock or 0), 4)
    ingredient.stock = counted
    db.session.add(StockMovement(tenant_id=ingredient.tenant_id, ingredient_id=ingredient.id, delta=delta, reason='stocktake',
                                 note=str(note or '盤點')[:200], staff_username=_staff()))
    if delta > 0:
        _add_lot(ingredient.tenant_id, ingredient.id, delta, source='stocktake', note='盤盈', stock_after=counted)
    elif delta < 0:
        draw_lots(ingredient.id, -delta)
    if not ingredient.is_low:
        ingredient.low_alerted_at = None
    if delta:   # 盤虧到安全量以下時也要通知 (不必等下一次扣料)
        deferred.defer(check_low_stock, ingredient.tenant_id, [ingredient.id])
    return delta


def check_low_stock(tenant_id, ingredient_ids=None):
    """低於安全量且尚未通知過的原料：通知一次；回到安全量以上時重設"""
    from app.services import monitor_service
    q = Ingredient.query.filter(Ingredient.tenant_id == tenant_id).execution_options(skip_tenant_filter=True)
    if ingredient_ids:
        q = q.filter(Ingredient.id.in_(ingredient_ids))
    alerted = []
    for ing in q.all():
        if ing.is_low and ing.low_alerted_at is None:
            ing.low_alerted_at = datetime.now()
            alerted.append(ing)
        elif not ing.is_low and ing.low_alerted_at is not None:
            ing.low_alerted_at = None
    db.session.commit()
    if alerted and settings_service.get('inventory', tenant_id).get('alert_notify', True):
        names = '、'.join(f'{i.name} (剩 {round(i.stock, 2)} {i.unit})' for i in alerted)
        monitor_service.alert(f'原料低於安全量：{names}', key=f'low-stock-{tenant_id}-{names[:40]}', tenant_id=tenant_id,
                              level='warning', source='inventory')
    return alerted


def item_unit_cost(menu_item):
    """每份餐點的原料成本 (有配方時)；沒有配方時使用餐點設定的單位成本 (多個餐點請直接用 report_service.unit_costs)"""
    from app.services.report_service import unit_costs
    return round(unit_costs(menu_item.tenant_id).get(menu_item.id, float(menu_item.unit_cost or 0)), 2)


def shortage_items(tenant_id):
    """原料不足以做一份的餐點 ID (禁止販售模式下，點餐頁顯示為售完)"""
    if not enabled(tenant_id) or not settings_service.get('inventory', tenant_id).get('block_when_short'):
        return set()
    stock = {i.id: i.stock or 0 for i in Ingredient.query.all()}
    short = set()
    for rl in RecipeLine.query.all():
        if stock.get(rl.ingredient_id, 0) < (rl.quantity or 0) - EPS:
            short.add(rl.menu_item_id)
    return short


# ---------------------------------------------------------------------------
# 批號效期 (先到期先出)
# ---------------------------------------------------------------------------
def _add_lot(tenant_id, ingredient_id, qty, expires_on=None, unit_cost=0, source='purchase', note='', stock_after=None):
    """建立批號；stock_after (加入後的庫存) 有值時，剩餘量不超過它：
    庫存為負 (先賣後補登進貨) 時，進貨量要先抵掉負數，批號剩餘量總和才不會大於原料庫存"""
    qty = round(float(qty or 0), 4)
    remaining = qty if stock_after is None else min(qty, round(max(0.0, float(stock_after)), 4))
    if qty <= 0 or remaining <= 0:
        return None
    lot = IngredientLot(tenant_id=tenant_id, ingredient_id=ingredient_id, quantity=qty, remaining=remaining,
                        expires_on=expires_on, unit_cost=unit_cost or 0, source=source, note=str(note or '')[:100],
                        received_at=datetime.now())
    db.session.add(lot)
    return lot


def _draw(ingredient_id, qty):
    """依「有效期限最早 (未填期限的最後)、進貨最早」扣減各批剩餘量；回傳 (實際扣到的量, 扣用批號中最早的有效期限)"""
    left = round(float(qty or 0), 4)
    if left <= 0:
        return 0, None
    db.session.flush()
    lots = IngredientLot.query.filter(IngredientLot.ingredient_id == ingredient_id, IngredientLot.remaining > 0) \
        .order_by(IngredientLot.expires_on.is_(None), IngredientLot.expires_on, IngredientLot.received_at, IngredientLot.id) \
        .execution_options(skip_tenant_filter=True).all()
    taken, earliest = 0.0, None
    for lot in lots:
        if left <= 0:
            break
        take = min(lot.remaining, left)
        lot.remaining = round(lot.remaining - take, 4)
        left = round(left - take, 4)
        taken += take
        if lot.expires_on is not None and (earliest is None or lot.expires_on < earliest):
            earliest = lot.expires_on
    return round(taken, 4), earliest


def draw_lots(ingredient_id, qty):
    """依「有效期限最早 (未填期限的最後)、進貨最早」扣減各批剩餘量；回傳實際扣到的量"""
    return _draw(ingredient_id, qty)[0]


def expiring_lots(tenant_id, days=None, today=None):
    """剩餘量 > 0 且在 days 天內到期 (含已過期) 的批號，最早到期的在前"""
    today = today or date.today()
    if days is None:
        v = settings_service.get('inventory', tenant_id).get('expiry_warn_days')
        days = 3 if v in (None, '') else max(0, safe_int(v, 3))   # 0 = 只提醒已到期 / 當天
    return IngredientLot.query.filter(IngredientLot.tenant_id == tenant_id, IngredientLot.remaining > 0,
                                      IngredientLot.expires_on.isnot(None),
                                      IngredientLot.expires_on <= today + timedelta(days=days)) \
        .order_by(IngredientLot.expires_on).execution_options(skip_tenant_filter=True).all()


def check_expiring(tenant_id, today=None):
    """每日：即將到期 / 已過期且尚未通知的批號通知一次；回傳通知筆數"""
    from app.services import monitor_service
    if not enabled(tenant_id):
        return 0
    lots = [lot for lot in expiring_lots(tenant_id, today=today) if lot.alerted_at is None]
    if not lots:
        return 0
    today = today or date.today()
    names = '、'.join(f'{lot.ingredient.name} {round(lot.remaining, 2)}{lot.ingredient.unit} '
                     f'({"已過期" if lot.expires_on < today else lot.expires_on.strftime("%m/%d") + " 到期"})'
                     for lot in lots[:20])
    for lot in lots:
        lot.alerted_at = datetime.now()
    db.session.commit()
    monitor_service.alert(f'原料效期提醒：{names}', key=f'lot-expiry-{tenant_id}-{today}', tenant_id=tenant_id,
                          level='warning', source='inventory')
    return len(lots)


def discard_lot(lot, note=''):
    """整批報廢 (過期)：扣減原料庫存並記報廢異動；回傳實際扣減的庫存量。由呼叫端 commit"""
    read = lot.remaining or 0
    if round(read, 4) <= 0:
        raise AppError('此批號已無剩餘量')
    # 條件式搶占：兩人同時按「整批報廢」時只有一個成功 (剩餘量已被別人改過就不扣)
    claimed = db.session.execute(update(IngredientLot).where(IngredientLot.id == lot.id, IngredientLot.remaining == read)
                                 .values(remaining=0).execution_options(synchronize_session=False)).rowcount
    if claimed != 1:
        raise AppError('此批號已被處理，請重新整理頁面', 409)
    set_committed_value(lot, 'remaining', 0)
    # 不把原料庫存扣成負數 (庫存曾為負時，批號剩餘量可能大於實際庫存)
    qty = round(min(read, max(0.0, _stock_now(lot.ingredient_id))), 4)
    if qty > 0:
        db.session.execute(update(Ingredient).where(Ingredient.id == lot.ingredient_id)
                           .values(stock=_stock_plus(-qty)).execution_options(synchronize_session=False))
        db.session.add(StockMovement(tenant_id=lot.tenant_id, ingredient_id=lot.ingredient_id, delta=-qty, reason='waste',
                                     note=(note or f'批號過期報廢 (有效期限 {lot.expires_on or "-"})')[:200],
                                     staff_username=_staff()))
        deferred.defer(check_low_stock, lot.tenant_id, [lot.ingredient_id])
    return qty
