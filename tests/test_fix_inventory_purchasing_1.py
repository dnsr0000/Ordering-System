"""修正小組 inventory-purchasing-1 的回歸測試：原物料庫存 / 批號 / 原料頁 / 採購單"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, IngredientLot, PurchaseOrder, PurchaseOrderLine, RecipeLine, StockMovement
from app.models.menu import MenuItem
from tests.support import assert_flash, cancel_order, default_tenant_id, line, make_item, tenant_request


def _add(admin_client, name, stock=0, low=0, cost=0, unit='g', **extra):
    return admin_client.post('/admin/inventory', data={'action': 'add', 'name': name, 'unit': unit, 'stock': str(stock),
                                                       'low_threshold': str(low), 'unit_cost': str(cost), **extra})


def _ing(app, name):
    with app.app_context():
        return Ingredient.query.filter_by(name=name).one()


def _recipe(admin_client, item_id, ing_ids, qtys):
    admin_client.post('/admin/inventory', data={'action': 'recipe', 'menu_item_id': item_id,
                                                'ingredient_id': [str(i) for i in ing_ids], 'quantity': [str(q) for q in qtys]})


def _pos(client, items):
    r = client.post('/api/pos/checkout', json={'items': items, 'payment_method': 'Cash', 'order_type': '內用',
                                                 'cash_received': 100000})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['order_id']


def test_fractional_consume_sells_last_portion(app, admin_client, store_settings):
    """B-398：0.1 kg 累減 30 次，最後一份不會被誤判為原料不足"""
    store_settings('inventory', enabled=True, block_when_short=True)
    _add(admin_client, '修一咖啡豆', stock=3, unit='kg')
    bean = _ing(app, '修一咖啡豆').id
    iid = make_item(app, '修一美式', price=60, stock=1000)
    _recipe(admin_client, iid, [bean], [0.1])
    tid = default_tenant_id(app)
    from app.services import inventory_service
    with tenant_request(app, tid):
        for _ in range(29):
            inventory_service.consume(tid, None, {iid: 1})
            db.session.commit()
        assert iid not in inventory_service.shortage_items(tid)
        inventory_service.consume(tid, None, {iid: 1})
        db.session.commit()
        assert abs(db.session.get(Ingredient, bean).stock) < 1e-9
        with pytest.raises(AppError):
            inventory_service.consume(tid, None, {iid: 1})
        db.session.rollback()


def test_restore_lot_keeps_expiry_and_negative_stock_lots(app, admin_client, staff_client, store_settings):
    """B-403：取消回補沿用扣用批號的效期；R2-150：負庫存後進貨 / 盤點的批號先抵負數，整批報廢不扣成負數"""
    store_settings('inventory', enabled=True, block_when_short=False)
    _add(admin_client, '修一牛奶', unit='ml')
    milk = _ing(app, '修一牛奶').id
    far, near = date.today() + timedelta(days=10), date.today() + timedelta(days=2)
    for exp in (far, near):
        admin_client.post('/admin/inventory', data={'action': 'purchase', 'ingredient_id': milk, 'quantity': '100',
                                                    'expires_on': exp.isoformat()})
    latte = make_item(app, '修一拿鐵', price=80, stock=1000)
    _recipe(admin_client, latte, [milk], [60])
    oid = _pos(staff_client, [line(latte)])
    assert cancel_order(admin_client, oid).status_code == 200
    with app.app_context():
        restored = IngredientLot.query.filter_by(ingredient_id=milk, source='restore').one()
        assert restored.remaining == 60 and restored.expires_on == near

    # 負庫存：200 → 賣 6 杯 (360) → -160；進貨 1000 → 批號剩 840
    _pos(staff_client, [line(latte, 6)])
    assert _ing(app, '修一牛奶').stock == -160
    admin_client.post('/admin/inventory', data={'action': 'purchase', 'ingredient_id': milk, 'quantity': '1000',
                                                'expires_on': (date.today() + timedelta(days=1)).isoformat()})
    with app.app_context():
        lot = IngredientLot.query.filter_by(ingredient_id=milk, source='purchase', quantity=1000).one()
        assert lot.remaining == 840 and db.session.get(Ingredient, milk).stock == 840
        lot_id = lot.id
        db.session.execute(update(Ingredient).where(Ingredient.id == milk).values(stock=500))   # 模擬實際庫存少於批號
        db.session.commit()
    admin_client.post('/admin/inventory', data={'action': 'discard_lot', 'lot_id': str(lot_id)})
    assert _ing(app, '修一牛奶').stock == 0, '整批報廢最多扣到 0'

    _pos(staff_client, [line(latte, 5)])   # 0 → -300
    admin_client.post('/admin/inventory', data={'action': 'stocktake', f'count__{milk}': '500'})
    with app.app_context():
        assert db.session.get(Ingredient, milk).stock == 500
        lot = IngredientLot.query.filter_by(ingredient_id=milk, source='stocktake').one()
        assert lot.remaining == 500


def test_receive_two_lines_same_ingredient_weighted_cost(app, admin_client, store_settings):
    """B-404：同一張採購單兩列相同原料，加權平均成本以累計庫存計算；update_lines 加入同原料時併入原列"""
    store_settings('inventory', enabled=True)
    _add(admin_client, '修一牛腱')
    beef = _ing(app, '修一牛腱').id
    tid = default_tenant_id(app)
    with app.app_context():
        po = PurchaseOrder(tenant_id=tid, status='draft', created_at=datetime.now())
        po.lines = [PurchaseOrderLine(tenant_id=tid, ingredient_id=beef, quantity=10, unit_cost=100),
                    PurchaseOrderLine(tenant_id=tid, ingredient_id=beef, quantity=10, unit_cost=200)]
        db.session.add(po)
        db.session.commit()
        po_id = po.id
    admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'receive'})
    b = _ing(app, '修一牛腱')
    assert b.stock == 20 and b.unit_cost == 150

    with app.app_context():
        po = PurchaseOrder(tenant_id=tid, status='draft', created_at=datetime.now())
        po.lines = [PurchaseOrderLine(tenant_id=tid, ingredient_id=beef, quantity=5, unit_cost=100)]
        db.session.add(po)
        db.session.commit()
        po_id, line_id = po.id, po.lines[0].id
    admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'save', f'qty__{line_id}': '5',
                                                            'add_ingredient_id': str(beef), 'add_qty': '3'})
    with app.app_context():
        lines = db.session.get(PurchaseOrder, po_id).lines
        assert [(ln.ingredient_id, ln.quantity) for ln in lines] == [(beef, 8)]


def test_stocktake_low_alert_negative_and_expiry_days_zero(app, admin_client, store_settings):
    """B-405：盤虧到安全量以下會通知；B-401：盤點負數被拒；B-406：效期提醒天數 0 不被當成 3"""
    store_settings('inventory', enabled=True, expiry_warn_days=0)
    _add(admin_client, '修一鮮奶油', stock=3000, low=1000, unit='ml')
    cream = _ing(app, '修一鮮奶油')
    assert cream.low_alerted_at is None
    admin_client.post('/admin/inventory', data={'action': 'stocktake', f'count__{cream.id}': '500'})
    cream = _ing(app, '修一鮮奶油')
    assert cream.stock == 500 and cream.low_alerted_at is not None

    admin_client.post('/admin/inventory', data={'action': 'stocktake', f'count__{cream.id}': '-5'})
    assert_flash(admin_client, '不可為負數')
    assert _ing(app, '修一鮮奶油').stock == 500

    for days in (0, 2):
        admin_client.post('/admin/inventory', data={'action': 'purchase', 'ingredient_id': cream.id, 'quantity': '10',
                                                    'expires_on': (date.today() + timedelta(days=days)).isoformat()})
    from app.services import inventory_service
    tid = default_tenant_id(app)
    with app.app_context():
        exps = {lot.expires_on for lot in inventory_service.expiring_lots(tid) if lot.ingredient_id == cream.id}
        assert exps == {date.today()}


def test_discard_lot_claimed_once(app, admin_client, store_settings):
    """B-407：另一人已整批報廢 (剩餘量已變) 時不再扣庫存"""
    store_settings('inventory', enabled=True)
    _add(admin_client, '修一起司')
    cheese = _ing(app, '修一起司').id
    admin_client.post('/admin/inventory', data={'action': 'purchase', 'ingredient_id': cheese, 'quantity': '10',
                                                'expires_on': date.today().isoformat()})
    from app.services import inventory_service
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        lot = IngredientLot.query.filter_by(ingredient_id=cheese).one()
        assert lot.remaining == 10
        db.session.execute(update(IngredientLot).where(IngredientLot.id == lot.id).values(remaining=0)
                           .execution_options(synchronize_session=False))   # 另一個請求先報廢
        with pytest.raises(AppError) as exc:
            inventory_service.discard_lot(lot)
        assert exc.value.status == 409
        db.session.rollback()
    assert _ing(app, '修一起司').stock == 10


def test_ingredient_edit_delete_recipe_and_opening_stock(app, admin_client, store_settings):
    """B-395 改名重複 / 空白；B-396 有紀錄的原料不可刪；B-400 刪除後重算碳足跡；B-397 配方重複原料合併；R2-151 期初庫存"""
    store_settings('inventory', enabled=True)
    _add(admin_client, '修一雞肉', stock=5000, low=6000, unit='g')
    _add(admin_client, '修一牛肉')
    chicken, beef = _ing(app, '修一雞肉'), _ing(app, '修一牛肉')
    with app.app_context():
        mv = StockMovement.query.filter_by(ingredient_id=chicken.id).one()
        assert mv.delta == 5000 and mv.note == '期初庫存'
        assert IngredientLot.query.filter_by(ingredient_id=chicken.id).one().remaining == 5000
    assert chicken.stock == 5000 and chicken.low_alerted_at is not None, '期初就低於安全量要通知'
    _add(admin_client, '修一負庫存', stock=-100)
    assert_flash(admin_client, '不可為負數')
    with app.app_context():
        assert Ingredient.query.filter_by(name='修一負庫存').first() is None

    base = {'action': 'edit', 'ingredient_id': chicken.id, 'unit': 'g', 'low_threshold': '6000', 'unit_cost': '0',
            'is_active': '1'}
    r = admin_client.post('/admin/inventory', data=dict(base, name='修一牛肉'))
    assert r.status_code == 302
    assert_flash(admin_client, '已存在')
    admin_client.post('/admin/inventory', data=dict(base, name='   '))
    assert _ing(app, '修一雞肉').name == '修一雞肉'

    admin_client.post('/admin/inventory', data={'action': 'delete', 'ingredient_id': chicken.id})
    assert_flash(admin_client, '無法刪除')
    with app.app_context():
        assert db.session.get(Ingredient, chicken.id) is not None

    admin_client.post('/admin/inventory', data={'action': 'edit', 'ingredient_id': beef.id, 'name': '修一牛肉', 'unit': 'g',
                                                'low_threshold': '0', 'unit_cost': '0', 'is_active': '1',
                                                'carbon_factor': '60'})
    iid = make_item(app, '修一牛肉麵', price=180)
    _recipe(admin_client, iid, [beef.id, beef.id], [100, 50])
    with app.app_context():
        assert [rl.quantity for rl in RecipeLine.query.filter_by(menu_item_id=iid)] == [150]
        assert db.session.get(MenuItem, iid).carbon_grams == 9000
    admin_client.post('/admin/inventory', data={'action': 'delete', 'ingredient_id': beef.id})
    with app.app_context():
        assert db.session.get(Ingredient, beef.id) is None
        assert db.session.get(MenuItem, iid).carbon_grams == 0


def test_suggest_qty_rounds_after_pending_and_save_claim(app, admin_client, store_settings):
    """R2-152：扣掉未收貨數量後再進位到採購單位；R2-153：儲存時採購單已被送出就不修改"""
    store_settings('inventory', enabled=True)
    _add(admin_client, '修一鮮奶', stock=800, low=1000, unit='ml')
    milk = _ing(app, '修一鮮奶').id
    admin_client.post('/admin/inventory', data={'action': 'edit', 'ingredient_id': milk, 'name': '修一鮮奶', 'unit': 'ml',
                                                'low_threshold': '1000', 'unit_cost': '0', 'is_active': '1',
                                                'par_level': '3000', 'pack_size': '1000'})
    tid = default_tenant_id(app)
    with app.app_context():
        po = PurchaseOrder(tenant_id=tid, status='draft', created_at=datetime.now())
        po.lines = [PurchaseOrderLine(tenant_id=tid, ingredient_id=milk, quantity=500, unit_cost=0)]
        db.session.add(po)
        db.session.commit()
        po_id, line_id = po.id, po.lines[0].id
    from app.services import purchasing_service
    with tenant_request(app, tid):
        assert dict((i.id, q) for i, q in purchasing_service.suggestions()).get(milk) == 2000

        po = db.session.get(PurchaseOrder, po_id)
        assert po.status == 'draft'
        db.session.execute(update(PurchaseOrder).where(PurchaseOrder.id == po_id).values(status='sent')
                           .execution_options(synchronize_session=False))
        db.session.commit()   # 另一人先送出
        set_committed_value(po, 'status', 'draft')   # 本請求先前讀到的仍是草稿
        with pytest.raises(AppError) as exc:
            purchasing_service.update_lines(po, {line_id: 1}, {})
        assert exc.value.status == 409
    with app.app_context():
        assert db.session.get(PurchaseOrderLine, line_id).quantity == 500


def test_update_lines_rejects_huge_qty_and_cost(app, admin_client, store_settings):
    """R2-193：採購單數量 / 單價 / 加入數量超過上限時拒絕，不存入極大值"""
    store_settings('inventory', enabled=True)
    _add(admin_client, '修一砂糖')
    sugar = _ing(app, '修一砂糖').id
    tid = default_tenant_id(app)
    with app.app_context():
        po = PurchaseOrder(tenant_id=tid, status='draft', created_at=datetime.now())
        po.lines = [PurchaseOrderLine(tenant_id=tid, ingredient_id=sugar, quantity=5, unit_cost=10)]
        db.session.add(po)
        db.session.commit()
        po_id, line_id = po.id, po.lines[0].id
    url = f'/admin/purchasing/po/{po_id}'
    for data in ({f'qty__{line_id}': '1e308'}, {f'cost__{line_id}': '1e308'},
                 {'add_ingredient_id': str(sugar), 'add_qty': '1e308'}):
        admin_client.post(url, data={'action': 'save', **data})
        assert_flash(admin_client, '過大')
        with app.app_context():
            ln = db.session.get(PurchaseOrderLine, line_id)
            assert (ln.quantity, ln.unit_cost) == (5, 10)
    assert admin_client.get(url).status_code == 200
    assert admin_client.get('/admin/purchasing').status_code == 200
    admin_client.post(url, data={'action': 'save', f'qty__{line_id}': '10000000', f'cost__{line_id}': '10000000'})
    with app.app_context():
        ln = db.session.get(PurchaseOrderLine, line_id)
        assert (ln.quantity, ln.unit_cost) == (10_000_000, 10_000_000)


def test_suggested_orders_skip_inactive_supplier(app, admin_client, store_settings):
    """B-409：預設供應商已停用的原料，建議採購單歸到「未指定」"""
    from app.models.inventory import Supplier
    from app.services import purchasing_service
    store_settings('inventory', enabled=True)
    _add(admin_client, '修一麵粉', stock=0, low=100, unit='g')
    flour = _ing(app, '修一麵粉').id
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        sup = Supplier(tenant_id=tid, name='修一停用供應商', is_active=False)
        db.session.add(sup)
        db.session.flush()
        db.session.get(Ingredient, flour).supplier_id = sup.id
        db.session.commit()
        created = purchasing_service.create_suggested_orders(tid, 'admin')
        mine = [po for po in created if any(ln.ingredient_id == flour for ln in po.lines)]
        assert len(mine) == 1 and mine[0].supplier_id is None
        db.session.rollback()


def test_consume_block_false_deducts_when_short(app, admin_client, store_settings):
    """B-189：block=False (分店收到已成立的線上訂單) 時原料不足也照扣，不拋 AppError"""
    store_settings('inventory', enabled=True, block_when_short=True)
    _add(admin_client, '修一抹茶粉', stock=5, unit='g')
    matcha = _ing(app, '修一抹茶粉').id
    iid = make_item(app, '修一抹茶拿鐵', price=90, stock=1000)
    _recipe(admin_client, iid, [matcha], [10])
    tid = default_tenant_id(app)
    from app.services import inventory_service
    with tenant_request(app, tid):
        with pytest.raises(AppError):
            inventory_service.consume(tid, None, {iid: 1})
        db.session.rollback()
        assert inventory_service.consume(tid, None, {iid: 1}, block=False) == {matcha: 10}
        db.session.commit()
        assert db.session.get(Ingredient, matcha).stock == -5
