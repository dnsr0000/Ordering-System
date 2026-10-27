"""
第五批：供應商與採購單 (建議採購 / 送出 / 收貨入庫)、原料批號效期 (先到期先出、到期提醒、整批報廢)、
備料預測、人事成本率、總部即時看板 (含分店彙整)。
"""
from datetime import date, datetime, timedelta

import pytest

from app.extensions import db
from app.models.inventory import Ingredient, IngredientLot, PurchaseOrder, StockMovement, Supplier
from app.models.order import Order
from tests.support import default_tenant_id, line, login_staff, make_item


def _ingredient(app, name):
    with app.app_context():
        return Ingredient.query.filter_by(name=name).first()


def _add_ing(admin_client, name, stock=0, low=0, cost=0, unit='g'):
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': name, 'unit': unit, 'stock': str(stock),
                                                'low_threshold': str(low), 'unit_cost': str(cost)})


def _pos(client, items, method='Cash'):
    r = client.post('/api/pos/checkout', json={'items': items, 'payment_method': method, 'order_type': '內用',
                                                 'cash_received': 100000})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['order_id']


# ===========================================================================
# 供應商與採購單
# ===========================================================================
def test_supplier_and_suggested_purchase_order_flow(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    r = admin_client.post('/admin/purchasing', data={'action': 'add_supplier', 'name': '大成肉品', 'phone': '02-1234',
                                                     'email': 'meat@example.com', 'lead_days': '2'})
    assert r.status_code == 302
    assert admin_client.post('/admin/purchasing', data={'action': 'add_supplier', 'name': '大成肉品'}).status_code in (302, 409)
    with app.app_context():
        sup = Supplier.query.filter_by(name='大成肉品').one()
        sid = sup.id
    _add_ing(admin_client, '採購雞腿', stock=200, low=1000, cost=0.2)
    ing = _ingredient(app, '採購雞腿')
    admin_client.post('/admin/inventory', data={'action': 'edit', 'ingredient_id': ing.id, 'name': '採購雞腿', 'unit': 'g',
                                                'low_threshold': '1000', 'unit_cost': '0.2', 'is_active': '1',
                                                'supplier_id': str(sid), 'par_level': '2000', 'pack_size': '500'})
    page = admin_client.get('/admin/purchasing').get_data(as_text=True)
    assert '採購雞腿' in page and '2000 g' in page, '補到 2000 並進位到 500 g 一箱'

    admin_client.post('/admin/purchasing', data={'action': 'suggest'})
    with app.app_context():
        po = PurchaseOrder.query.filter_by(supplier_id=sid).order_by(PurchaseOrder.id.desc()).first()
        assert po.status == 'draft' and po.expected_on == date.today() + timedelta(days=2)
        assert [(ln.ingredient.name, ln.quantity) for ln in po.lines] == [('採購雞腿', 2000)]
        po_id, line_id = po.id, po.lines[0].id
    admin_client.post('/admin/purchasing', data={'action': 'suggest'})
    with app.app_context():
        assert PurchaseOrder.query.filter_by(supplier_id=sid).count() == 1, '已在採購單中的不重複建議'

    _add_ing(admin_client, '採購洋蔥', stock=0, unit='kg')
    onion = _ingredient(app, '採購洋蔥')
    admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'save', f'qty__{line_id}': '1500',
                                                            f'cost__{line_id}': '0.18', 'add_ingredient_id': str(onion.id),
                                                            'add_qty': '5', 'expected_on': '', 'note': '下午送',
                                                            'supplier_id': str(sid)})
    admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'send'})
    with app.app_context():
        po = db.session.get(PurchaseOrder, po_id)
        assert po.status == 'sent' and len(po.lines) == 2 and po.note == '下午送'
        onion_line = next(ln for ln in po.lines if ln.ingredient_id == onion.id).id
    r = admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'save', f'qty__{line_id}': '1'})
    assert r.status_code in (302, 400)
    with app.app_context():
        assert next(ln for ln in db.session.get(PurchaseOrder, po_id).lines if ln.id == line_id).quantity == 1500, '送出後不可改'
    assert '採購單' in admin_client.get(f'/admin/purchasing/po/{po_id}/print').get_data(as_text=True)

    exp = (date.today() + timedelta(days=5)).isoformat()
    admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'receive', f'recv__{line_id}': '1400',
                                                            f'exp__{line_id}': exp, f'recv__{onion_line}': '5'})
    with app.app_context():
        po = db.session.get(PurchaseOrder, po_id)
        assert po.status == 'received' and po.received_by
        chicken = Ingredient.query.filter_by(name='採購雞腿').one()
        assert chicken.stock == 1600
        lot = IngredientLot.query.filter_by(ingredient_id=chicken.id).one()
        assert (lot.quantity, lot.remaining, lot.expires_on.isoformat()) == (1400, 1400, exp)
        assert StockMovement.query.filter_by(ingredient_id=chicken.id, reason='purchase').one().note == f'採購單 {po.number}'
    r = admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'cancel'})
    with app.app_context():
        assert db.session.get(PurchaseOrder, po_id).status == 'received', '已收貨不可取消'


def test_purchasing_requires_inventory_permission(app):
    staff = login_staff(app, 'po_staff', 'staff')
    assert staff.get('/admin/purchasing').status_code in (302, 403)
    kitchen = login_staff(app, 'po_kitchen', 'kitchen')
    assert kitchen.get('/admin/purchasing').status_code == 200, '廚房角色預設有原物料權限'


# ===========================================================================
# 批號效期
# ===========================================================================
def test_lots_first_expiry_first_out_alert_and_discard(app, admin_client, staff_client, store_settings, monkeypatch):
    from app.services import inventory_service, monitor_service
    store_settings('inventory', enabled=True, expiry_warn_days=3)
    _add_ing(admin_client, '效期牛奶', unit='ml')
    milk = _ingredient(app, '效期牛奶')
    far, near = date.today() + timedelta(days=10), date.today() + timedelta(days=2)
    for exp in (far, near):
        admin_client.post('/admin/inventory', data={'action': 'purchase', 'ingredient_id': milk.id, 'quantity': '100',
                                                    'expires_on': exp.isoformat()})
    latte = make_item(app, '效期拿鐵', price=80)
    admin_client.post('/admin/inventory', data={'action': 'recipe', 'menu_item_id': latte, 'ingredient_id': [milk.id],
                                                'quantity': ['30']})
    oid = _pos(staff_client, [line(latte, 2)])

    def lots():
        with app.app_context():
            return {l.expires_on: l.remaining for l in IngredientLot.query.filter_by(ingredient_id=milk.id).all()}
    assert lots() == {far: 100, near: 40}, '先到期的批號先扣'
    admin_client.post('/admin/inventory', data={'action': 'waste', 'ingredient_id': milk.id, 'quantity': '50'})
    assert lots() == {far: 90, near: 0}

    page = admin_client.get('/admin/inventory').get_data(as_text=True)
    assert 'id="expiringLots"' not in page, '近到期批號已用完'
    with app.app_context():
        lot = IngredientLot.query.filter_by(ingredient_id=milk.id, expires_on=far).one()
        lot.expires_on = date.today() - timedelta(days=1)
        db.session.commit()
        lot_id = lot.id
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append(msg))
    with app.app_context():
        assert inventory_service.check_expiring(default_tenant_id(app)) >= 1
        assert inventory_service.check_expiring(default_tenant_id(app)) == 0, '同一批只提醒一次'
    assert any('效期牛奶' in a and '已過期' in a for a in alerts)
    page = admin_client.get('/admin/inventory').get_data(as_text=True)
    assert 'id="expiringLots"' in page and '已過期' in page
    admin_client.post('/admin/inventory', data={'action': 'discard_lot', 'lot_id': str(lot_id)})
    with app.app_context():
        assert db.session.get(IngredientLot, lot_id).remaining == 0
        assert Ingredient.query.filter_by(name='效期牛奶').one().stock == 200 - 60 - 50 - 90

    from tests.support import cancel_order
    assert cancel_order(admin_client, oid).status_code == 200
    with app.app_context():
        restored = IngredientLot.query.filter_by(ingredient_id=milk.id, source='restore').one()
        assert restored.remaining == 60 and restored.expires_on is None


# ===========================================================================
# 備料預測
# ===========================================================================
def test_forecast_uses_same_weekday_history(app, admin_client, staff_client, store_settings):
    store_settings('inventory', enabled=False)
    target = date.today() + timedelta(days=1)
    soup = make_item(app, '預測濃湯', price=60, category='湯品')
    _add_ing(admin_client, '預測馬鈴薯', stock=100)
    potato = _ingredient(app, '預測馬鈴薯')
    admin_client.post('/admin/inventory', data={'action': 'recipe', 'menu_item_id': soup, 'ingredient_id': [potato.id],
                                                'quantity': ['50']})
    for weeks_ago, qty in ((1, 3), (2, 5)):
        oid = _pos(staff_client, [line(soup, qty)])
        with app.app_context():
            o = db.session.get(Order, oid)
            o.created_at = datetime.combine(target - timedelta(days=7 * weeks_ago), datetime.min.time()) + timedelta(hours=12)
            db.session.commit()
    from app.services import forecast_service
    with app.test_request_context('/'):
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            f = forecast_service.forecast(target, weeks=2)
    row = next(r for r in f['items'] if r['name'] == '預測濃湯')
    assert (row['qty'], row['avg'], row['history']) == (4, 4.0, [5, 3])
    need = next(i for i in f['ingredients'] if i['name'] == '預測馬鈴薯')
    assert need['need'] == 200 and need['short'] == 100
    page = admin_client.get(f'/admin/forecast?date={target.isoformat()}&weeks=2').get_data(as_text=True)
    assert '預測濃湯' in page and 'id="forecastIngredients"' in page


# ===========================================================================
# 人事成本率
# ===========================================================================
def test_labor_cost_ratio_in_report(app, admin_client):
    from app.models.staff import TimeClock
    from app.models.tenant import StaffAccount
    from app.services import report_service
    login_staff(app, 'labor_a', 'staff')
    login_staff(app, 'labor_b', 'staff')
    with app.app_context():
        a = StaffAccount.query.filter_by(username='labor_a').one().id
        b = StaffAccount.query.filter_by(username='labor_b').one().id
    r = admin_client.post('/admin/staff', data={'action': 'wage', 'account_id': str(a), 'hourly_wage': '200'})
    assert r.status_code == 302
    day = date(2026, 3, 2)
    with app.app_context():
        assert db.session.get(StaffAccount, a).hourly_wage == 200
        tid = default_tenant_id(app)
        for sid in (a, b):
            db.session.add(TimeClock(tenant_id=tid, staff_account_id=sid, clock_in=datetime(2026, 3, 2, 9),
                                     clock_out=datetime(2026, 3, 2, 17)))
        db.session.commit()
        rep = report_service.build_report(tid, day, day)
        assert rep['labor_hours'] == 16 and rep['labor_cost'] == 1600
        assert rep['labor_missing_wage'] == ['labor_b']
    page = admin_client.get('/admin/reports?start=2026-03-02&end=2026-03-02').get_data(as_text=True)
    assert 'id="laborSection"' in page and 'labor_b' in page and '人事成本率' in page
    assert 'staff-wage-row' in admin_client.get('/admin/staff').get_data(as_text=True)


# ===========================================================================
# 即時看板
# ===========================================================================
def test_live_dashboard_local(app, admin_client, staff_client, manager_client):
    item = make_item(app, '看板便當', price=111)
    _pos(staff_client, [line(item)])
    page = manager_client.get('/admin/live')
    assert page.status_code == 200 and 'js/live-dashboard.js' in page.get_data(as_text=True)
    data = manager_client.get('/api/admin/live').get_json()
    assert data['success'] and data['orders'] >= 1 and data['revenue'] >= 111
    assert any(s['name'] == '本店' for s in data['stores'])
    assert 1 <= len(data['top_items']) <= 10 and data['top_items'][0]['qty'] >= 1
    assert len(data['hourly']) == 24
    assert staff_client.get('/api/admin/live').status_code in (302, 403)
    assert 'id="nav-live"' in admin_client.get('/admin').get_data(as_text=True)


def test_live_dashboard_on_hub_groups_branches():
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, tenant_ctx
    env = Env()
    try:
        iid = _add_item(env.a, '看板分店餐', price=150, stock=20)
        env.sync_all()
        _checkout(env.a, [line(_item(env.a, iid)['id'], 2)])
        _checkout(env.b, [line(_item(env.b, iid)['id'], 1)])
        env.sync_all()
        from app.services import live_dashboard_service
        with tenant_ctx(env.hub) as tenant:
            data = live_dashboard_service.snapshot(tenant.id)
        stores = {s['name']: s for s in data['stores']}
        assert stores['台南店']['revenue'] == 300 and stores['高雄店']['revenue'] == 150
        assert data['is_hub'] and data['revenue'] == 450
    finally:
        env.close()
