"""
多分店營運 (第三輪 #7、#10)：

分店價格 / 分店上下架
- 總部 (線上端) 在「分店價格 / 上下架」頁為各分店設定餐點售價或下架，只同步給該分店 (BranchMenuOverride)。
- 點餐頁、POS、桌邊掃碼與結帳時以 activate() 套用：分店價取代總部內用價，通路價與特價依同一個價差加減；
  下架的餐點不顯示、不能結帳 (套餐配餐被下架時該套餐不可選)。線上點餐依顧客選擇的取餐分店套用。

分店叫貨 / 中央廚房 / 分店間調撥 (StockTransfer)
- 叫貨：收貨方建立 → 出貨方出貨 (扣自己的原料庫存) 或拒絕 → 收貨方收貨 (加到自己的原料庫存，可填實收數量)。
- 調撥：出貨方直接建立並出貨，收貨方收貨。
- 「總部 / 中央廚房」是線上端主機自己的原料庫存 (site = 'HQ')；分店間的單據經線上端同步給雙方。
- 原料依名稱對應；收貨方沒有這項原料時自動建立 (單位沿用出貨方)。出貨方的單位成本隨單據帶到收貨方加權平均。
- 狀態以條件式 UPDATE 推進 (兩個人同時按出貨只會扣一次庫存)；同步時狀態只往前走 (見 app/sync/engine.py)。
"""
import json
from datetime import datetime

from flask import g, has_request_context
from flask_login import current_user
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.branch import HQ_SITE, BranchMenuOverride, StockTransfer
from app.models.inventory import Ingredient
from app.models.menu import MenuItem
from app.models.settings import HqSetting
from app.sync import runtime as sync_runtime
from app.utils import safe_float, safe_int

SITES_KEY = 'branch_sites'
HQ_NAME = '總部 / 中央廚房'
MAX_LINES = 50


def _staff():
    if has_request_context() and getattr(current_user, 'is_authenticated', False):
        return current_user.username
    return 'system'


# ---------------------------------------------------------------------------
# 分店名單 (線上端寫入 HqSetting 下發，分店建立叫貨 / 調撥單時選擇對象)
# ---------------------------------------------------------------------------
def refresh_sites(tenant_id, only_if_missing=False):
    """線上端：把分店名單寫入 HqSetting (有變更才寫)；回傳是否有寫入。由呼叫端 commit。"""
    if not sync_runtime.is_hub():
        return False
    from app.models.sync import SyncNode
    row = HqSetting.query.filter_by(tenant_id=tenant_id, key=SITES_KEY).execution_options(skip_tenant_filter=True).first()
    if row is not None and only_if_missing:
        return False
    nodes = SyncNode.query.filter_by(tenant_id=tenant_id).execution_options(skip_tenant_filter=True) \
        .order_by(SyncNode.id).all()
    value = json.dumps([{'uuid': n.uuid, 'name': n.name, 'active': bool(n.is_active)} for n in nodes], ensure_ascii=False)
    if row is None:
        db.session.add(HqSetting(tenant_id=tenant_id, key=SITES_KEY, value=value, changed_by='system'))
    elif row.value != value:
        row.value, row.changed_at = value, datetime.now()
    else:
        return False
    return True


def sites(tenant_id):
    """[{uuid, name, active}]：總部 / 中央廚房在第一個；分店名單在線上端直接讀取，分店讀總部下發的名單"""
    out = [{'uuid': HQ_SITE, 'name': HQ_NAME, 'active': True}]
    if sync_runtime.is_hub():
        from app.models.sync import SyncNode
        out += [{'uuid': n.uuid, 'name': n.name, 'active': bool(n.is_active)}
                for n in SyncNode.query.filter_by(tenant_id=tenant_id).order_by(SyncNode.id).all()]
        return out
    row = HqSetting.query.filter_by(tenant_id=tenant_id, key=SITES_KEY).first()
    try:
        rows = json.loads(row.value) if row is not None else []
    except ValueError:
        rows = []
    out += [r for r in rows if isinstance(r, dict) and r.get('uuid')]
    return out


def local_site():
    """本機在叫貨 / 調撥中的身分：線上端 = 總部 'HQ'、分店 = 分店 uuid、單機 = None (不提供此功能)"""
    if sync_runtime.is_hub():
        return HQ_SITE
    if sync_runtime.is_node():
        return sync_runtime.local_node_uuid()
    return None


def site_name(tenant_id, uuid):
    return next((s['name'] for s in sites(tenant_id) if s['uuid'] == uuid), '其他分店')


# ---------------------------------------------------------------------------
# 分店價格 / 上下架
# ---------------------------------------------------------------------------
def activate(tenant_id, node_uuid):
    """本次請求套用某分店的價格 / 上下架 (node_uuid 為空時清除，回到總部設定)"""
    if not node_uuid:
        g.branch_menu = {}
        return {}
    rows = db.session.execute(
        db.select(BranchMenuOverride.menu_item_id, BranchMenuOverride.price, BranchMenuOverride.is_hidden, MenuItem.price)
        .join(MenuItem, MenuItem.id == BranchMenuOverride.menu_item_id)
        .where(BranchMenuOverride.tenant_id == tenant_id, BranchMenuOverride.node_uuid == node_uuid,
               (BranchMenuOverride.price.is_not(None)) | BranchMenuOverride.is_hidden.is_(True))
        .execution_options(skip_tenant_filter=True)).all()
    data = {item_id: ((price - (base or 0)) if price is not None else 0, bool(hidden))
            for item_id, price, hidden, base in rows}
    g.branch_menu = data
    return data


def storefront_branch(pickup_node=None):
    """點餐頁 / POS 使用哪家分店的設定：分店 = 本分店；線上端 = 顧客選擇的取餐分店"""
    if sync_runtime.is_hub():
        return pickup_node.uuid if pickup_node is not None else None
    return sync_runtime.local_node_uuid()


def activate_for_request(tenant_id):
    """點餐頁的 API (促銷試算等)：分店用本分店；線上端用顧客在點餐頁選的取餐分店 (session)"""
    from flask import session
    if sync_runtime.is_hub():
        return activate(tenant_id, session.get('pickup_node_uuid') if has_request_context() else None)
    return activate(tenant_id, sync_runtime.local_node_uuid())


def overrides_for(tenant_id, node_uuid):
    """{menu_item_id: BranchMenuOverride}"""
    return {o.menu_item_id: o for o in BranchMenuOverride.query.filter_by(tenant_id=tenant_id, node_uuid=node_uuid).all()}


def save_overrides(tenant_id, node_uuid, rows, staff=''):
    """
    線上端：儲存某分店的價格 / 上下架。rows = [{menu_item_id, price (空白 = 同總部), hidden}]。
    回傳 (變更筆數, 說明清單)。由呼叫端 commit。
    """
    if not sync_runtime.is_hub():
        raise AppError('分店價格 / 上下架由總部 (線上端) 設定', 403)
    from app.models.sync import SyncNode
    node = SyncNode.query.filter_by(tenant_id=tenant_id, uuid=str(node_uuid or '')).first()
    if node is None:
        raise AppError('查無此分店', 404)
    if not isinstance(rows, list) or len(rows) > 2000:
        raise AppError('資料格式錯誤')
    items = {m.id: m for m in MenuItem.query.filter_by(tenant_id=tenant_id).all()}
    existing = overrides_for(tenant_id, node.uuid)
    changed, notes = 0, []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = items.get(safe_int(raw.get('menu_item_id'), 0))
        if item is None:
            raise AppError('包含不存在的餐點')
        price_raw = raw.get('price')
        price = None if price_raw in (None, '') else safe_int(price_raw, -1)
        if price is not None and not 0 <= price <= 100000:
            raise AppError(f'餐點【{item.name}】的分店價格不正確')
        if price is not None and price == item.price:
            price = None   # 與總部同價：不另外設定
        if price is not None:   # 特價 / 通路價依同一個價差加減：不可因此變成 0 元以下
            delta = price - (item.price or 0)
            others = [('特價', item.discount_price if item.is_discount else 0), ('外帶價', item.takeout_price),
                      ('線上價', item.online_price), ('外送價', item.delivery_price)]
            bad = [f'{label} ${v}→${v + delta}' for label, v in others if (v or 0) > 0 and v + delta <= 0]
            if bad:
                raise AppError(f'餐點【{item.name}】分店價比總部便宜 ${-delta}，會讓 {"、".join(bad)}；請調高分店價或先修改總部價格')
        hidden = bool(raw.get('hidden'))
        ov = existing.get(item.id)
        if ov is None:
            if price is None and not hidden:
                continue
            ov = BranchMenuOverride(tenant_id=tenant_id, node_uuid=node.uuid, menu_item_id=item.id)
            db.session.add(ov)
        elif ov.price == price and bool(ov.is_hidden) == hidden:
            continue
        ov.price, ov.is_hidden, ov.changed_by = price, hidden, staff
        changed += 1
        notes.append(f"{item.name}{f' ${price}' if price is not None else ''}{' 下架' if hidden else ''}"
                     if (price is not None or hidden) else f'{item.name} 恢復總部設定')
    return changed, notes


# ---------------------------------------------------------------------------
# 叫貨 / 調撥
# ---------------------------------------------------------------------------
def _require_site(tenant_id):
    me = local_site()
    if not me:
        raise AppError('叫貨 / 調撥需要啟用多分店同步 (線上端或分店)', 400)
    return me


def _clean_lines(tenant_id, raw_lines, need_stock=False):
    """[{name, qty}] → [{name, unit, qty, shipped, received, unit_cost}]；原料必須存在於本機 (出貨方另檢查庫存)"""
    if not isinstance(raw_lines, list) or not raw_lines:
        raise AppError('請至少加入一項原料')
    if len(raw_lines) > MAX_LINES:
        raise AppError(f'一張單最多 {MAX_LINES} 項原料')
    out, seen = [], set()
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get('name') or '').strip()[:60]
        qty = round(safe_float(raw.get('qty'), 0), 4)
        if not name or qty <= 0:
            continue
        if name in seen:
            raise AppError(f'原料【{name}】重複')
        seen.add(name)
        ing = Ingredient.query.filter_by(tenant_id=tenant_id, name=name).first()
        if ing is None:
            raise AppError(f'本店沒有原料【{name}】，請先在「原物料與配方」新增')
        out.append({'name': name, 'unit': ing.unit or '', 'qty': qty, 'shipped': None, 'received': None,
                    'unit_cost': round(ing.unit_cost or 0, 4)})
    if not out:
        raise AppError('請輸入原料數量')
    return out


def create_request(tenant_id, from_site, raw_lines, note='', staff=''):
    """叫貨：向總部中央廚房或其他分店叫貨 (本店收貨)"""
    me = _require_site(tenant_id)
    if from_site == me:
        raise AppError('不能向自己叫貨')
    site = next((s for s in sites(tenant_id) if s['uuid'] == from_site and s.get('active', True)), None)
    if site is None:
        raise AppError('請選擇出貨的總部或分店')
    lines = _clean_lines(tenant_id, raw_lines)
    tr = StockTransfer(tenant_id=tenant_id, kind='request', from_site=from_site, to_site=me, from_name=site['name'],
                       to_name=site_name(tenant_id, me), status='requested', note=str(note or '')[:200],
                       created_by=staff or _staff(), created_at=datetime.now())
    tr.set_lines(lines)
    db.session.add(tr)
    db.session.flush()
    return tr


def create_transfer(tenant_id, to_site, raw_lines, note='', staff=''):
    """調撥：本店直接出貨給其他分店 / 總部 (當下扣本店庫存)"""
    me = _require_site(tenant_id)
    if to_site == me:
        raise AppError('不能調撥給自己')
    site = next((s for s in sites(tenant_id) if s['uuid'] == to_site and s.get('active', True)), None)
    if site is None:
        raise AppError('請選擇收貨的總部或分店')
    lines = _clean_lines(tenant_id, raw_lines)
    staff = staff or _staff()
    tr = StockTransfer(tenant_id=tenant_id, kind='transfer', from_site=me, to_site=to_site,
                       from_name=site_name(tenant_id, me), to_name=site['name'], status='shipped',
                       note=str(note or '')[:200], created_by=staff, created_at=datetime.now(),
                       shipped_by=staff, shipped_at=datetime.now())
    for ln in lines:
        ln['shipped'] = ln['qty']
    tr.set_lines(lines)
    db.session.add(tr)
    db.session.flush()
    _deduct_for(tenant_id, tr, lines)
    tr.set_lines(lines)   # 帶上出貨方的單位成本
    return tr


def get_transfer(tenant_id, transfer_id):
    tr = db.session.get(StockTransfer, safe_int(transfer_id, 0))
    if tr is None or tr.tenant_id != tenant_id or local_site() not in (tr.from_site, tr.to_site):
        raise AppError('查無此叫貨 / 調撥單', 404)
    return tr


def _claim(tr, expected, **values):
    """條件式推進狀態：兩個人同時操作時只有一個成功"""
    ok = db.session.execute(update(StockTransfer).where(StockTransfer.id == tr.id, StockTransfer.status == expected)
                            .values(**values).execution_options(synchronize_session=False)).rowcount
    if not ok:
        db.session.refresh(tr)
        raise AppError(f'此單目前狀態為「{tr.status_label}」，無法執行 (可能剛被其他人處理)', 409)
    db.session.refresh(tr)


def _deduct_for(tenant_id, tr, lines):
    """出貨：扣本店原料庫存 (數量不足時整筆失敗)"""
    from app.services import inventory_service
    for ln in lines:
        qty = ln.get('shipped') or 0
        if qty <= 0:
            continue
        ing = Ingredient.query.filter_by(tenant_id=tenant_id, name=ln['name']).first()
        if ing is None:
            raise AppError(f'本店沒有原料【{ln["name"]}】，無法出貨')
        took = db.session.execute(update(Ingredient).where(Ingredient.id == ing.id, Ingredient.stock >= qty)
                                  .values(stock=Ingredient.stock - qty).execution_options(synchronize_session=False))
        if took.rowcount != 1:
            db.session.refresh(ing, ['stock'])
            raise AppError(f'原料【{ing.name}】庫存 {round(ing.stock or 0, 2)} {ing.unit}，不足出貨 {qty}')
        inventory_service.record_movement(ing, -qty, 'transfer_out', note=f'{tr.number} 出貨給{tr.to_name}',
                                          unit_cost=ing.unit_cost or 0)
        ln['unit_cost'] = round(ing.unit_cost or 0, 4)


def ship(tenant_id, transfer_id, shipped, staff=''):
    """出貨方出貨：shipped = {原料名稱: 實出數量} (沒有填的以叫貨數量出貨)；扣本店庫存"""
    tr = get_transfer(tenant_id, transfer_id)
    if tr.from_site != local_site():
        raise AppError('只有出貨方可以出貨', 403)
    lines = tr.lines
    for ln in lines:
        raw = (shipped or {}).get(ln['name'])
        ln['shipped'] = round(max(0.0, safe_float(raw, ln['qty'])), 4) if raw not in (None, '') else ln['qty']
    if not any(ln['shipped'] for ln in lines):
        raise AppError('出貨數量都是 0；不出貨請按「拒絕」')
    staff = staff or _staff()
    _claim(tr, 'requested', status='shipped', shipped_by=staff, shipped_at=datetime.now(),
           lines_json=json.dumps(lines, ensure_ascii=False))
    _deduct_for(tenant_id, tr, lines)
    tr.set_lines(lines)   # 帶上出貨方的單位成本
    return tr


def reject(tenant_id, transfer_id, reason, staff=''):
    tr = get_transfer(tenant_id, transfer_id)
    if tr.from_site != local_site():
        raise AppError('只有出貨方可以拒絕', 403)
    _claim(tr, 'requested', status='rejected', reason=str(reason or '無法出貨')[:200], shipped_by=staff or _staff())
    return tr


def cancel(tenant_id, transfer_id, reason='', staff=''):
    tr = get_transfer(tenant_id, transfer_id)
    if tr.to_site != local_site() or tr.kind != 'request':
        raise AppError('只有叫貨的分店可以取消叫貨單', 403)
    _claim(tr, 'requested', status='cancelled', reason=str(reason or '取消叫貨')[:200])
    return tr


def receive(tenant_id, transfer_id, received, staff='', note=''):
    """收貨方收貨：received = {原料名稱: 實收數量} (沒有填的以實出數量收貨)；加到本店庫存 (沒有的原料自動建立)"""
    from app.services import inventory_service
    tr = get_transfer(tenant_id, transfer_id)
    if tr.to_site != local_site():
        raise AppError('只有收貨方可以收貨', 403)
    lines = tr.lines
    diffs = []
    for ln in lines:
        raw = (received or {}).get(ln['name'])
        sent = ln.get('shipped') or 0
        ln['received'] = round(max(0.0, safe_float(raw, sent)), 4) if raw not in (None, '') else sent
        if abs(ln['received'] - sent) > 1e-9:
            diffs.append(f"{ln['name']} 出 {sent:g} 收 {ln['received']:g}")
    staff = staff or _staff()
    reason = '；'.join(filter(None, [str(note or '').strip()[:100], ('差異：' + '、'.join(diffs)) if diffs else '']))[:200]
    _claim(tr, 'shipped', status='received', received_by=staff, received_at=datetime.now(),
           lines_json=json.dumps(lines, ensure_ascii=False), reason=reason or tr.reason)
    for ln in lines:
        qty = ln['received']
        if qty <= 0:
            continue
        ing = Ingredient.query.filter_by(tenant_id=tenant_id, name=ln['name']).first()
        if ing is None:
            ing = Ingredient(tenant_id=tenant_id, name=ln['name'], unit=ln.get('unit') or 'g', stock=0)
            db.session.add(ing)
            db.session.flush()
        inventory_service.adjust(ing, qty, 'transfer_in', note=f'{tr.number} 收自{tr.from_name}',
                                 unit_cost=ln.get('unit_cost') or None)
    if diffs:   # 提交後通知 (交易失敗則不通知)
        from app.services import deferred, monitor_service
        deferred.defer(monitor_service.alert, f'{tr.number} 收貨數量與出貨不符：{"、".join(diffs)}',
                       key=f'transfer-diff-{tr.uuid}', tenant_id=tenant_id, level='warning', source='inventory')
    return tr


def transfers(tenant_id, limit=200):
    me = local_site()
    if not me:
        return []
    return StockTransfer.query.filter((StockTransfer.from_site == me) | (StockTransfer.to_site == me)) \
        .order_by(StockTransfer.id.desc()).limit(limit).all()
