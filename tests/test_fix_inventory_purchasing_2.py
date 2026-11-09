"""
修正小組 inventory-purchasing-2 的回歸測試：
採購單送出時套用未儲存的修改 (B-399)、收貨清空實收數量視為未到貨 (B-402)、停用供應商不被默默清掉 (B-409)、
建議採購空表文字 (B-410)、再點一次不把會員價剛好等於加購價的單點品項誤判為加購 (R2-154)。
"""
import pytest

from app.extensions import db
from app.models.inventory import Ingredient, PurchaseOrder, PurchaseOrderLine, Supplier
from tests.support import checkout, default_tenant_id, line, make_item, make_member, member_client

pytestmark = pytest.mark.usefixtures('_per_test_isolation')


def _ing(app, name, stock=0, low=0, cost=1.0, supplier_id=None):
    with app.app_context():
        ing = Ingredient(tenant_id=default_tenant_id(app), name=name, unit='g', stock=stock, low_threshold=low,
                         unit_cost=cost, supplier_id=supplier_id)
        db.session.add(ing)
        db.session.commit()
        return ing.id


def _supplier(app, name, active=True):
    with app.app_context():
        sup = Supplier(tenant_id=default_tenant_id(app), name=name, is_active=active)
        db.session.add(sup)
        db.session.commit()
        return sup.id


def _draft(app, ingredient_ids, supplier_id=None, qty=2000):
    with app.app_context():
        tid = default_tenant_id(app)
        po = PurchaseOrder(tenant_id=tid, supplier_id=supplier_id, status='draft', created_by='t')
        po.lines = [PurchaseOrderLine(tenant_id=tid, ingredient_id=i, quantity=qty, unit_cost=1) for i in ingredient_ids]
        db.session.add(po)
        db.session.commit()
        return po.id, [ln.id for ln in po.lines]


def test_b399_send_applies_unsaved_edits(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    beef = _ing(app, '修正399牛肉')
    po_id, (line_id,) = _draft(app, [beef])
    page = admin_client.get(f'/admin/purchasing/po/{po_id}').get_data(as_text=True)
    assert 'form="poSendForm"' not in page and 'name="action" value="send"' in page
    r = admin_client.post(f'/admin/purchasing/po/{po_id}', data={
        'action': 'send', f'qty__{line_id}': '1500', f'cost__{line_id}': '2', 'add_ingredient_id': '', 'add_qty': '',
        'expected_on': '', 'note': '修正後送出', 'supplier_id': ''})
    assert r.status_code == 302
    with app.app_context():
        po = db.session.get(PurchaseOrder, po_id)
        assert po.status == 'sent' and po.note == '修正後送出'
        assert [(ln.quantity, ln.unit_cost) for ln in po.lines] == [(1500, 2)]
    # 只帶 action=send 的舊版表單仍可直接送出
    po2, _ = _draft(app, [beef])
    admin_client.post(f'/admin/purchasing/po/{po2}', data={'action': 'send'})
    with app.app_context():
        po = db.session.get(PurchaseOrder, po2)
        assert po.status == 'sent' and po.lines[0].quantity == 2000


def test_b402_blank_received_qty_means_not_delivered(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    chicken = _ing(app, '修正402雞腿')
    onion = _ing(app, '修正402洋蔥')
    po_id, (l1, l2) = _draft(app, [chicken, onion], qty=500)
    r = admin_client.post(f'/admin/purchasing/po/{po_id}', data={'action': 'receive', f'recv__{l1}': '500',
                                                                f'recv__{l2}': '', f'exp__{l1}': '', f'exp__{l2}': ''})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(Ingredient, chicken).stock == 500
        assert db.session.get(Ingredient, onion).stock == 0
        assert db.session.get(PurchaseOrderLine, l2).received_qty == 0
    # 欄位完全沒送出：仍以訂購量收貨 (相容)
    po2, (l3,) = _draft(app, [onion], qty=300)
    admin_client.post(f'/admin/purchasing/po/{po2}', data={'action': 'receive'})
    with app.app_context():
        assert db.session.get(Ingredient, onion).stock == 300


def test_b409_inactive_supplier_is_kept_selected(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    sid = _supplier(app, '修正409停用肉行', active=False)
    beef = _ing(app, '修正409牛肉', supplier_id=sid)
    inv = admin_client.get('/admin/inventory').get_data(as_text=True)
    assert f'<option value="{sid}" selected>修正409停用肉行 (停用)</option>' in inv
    po_id, _ = _draft(app, [beef], supplier_id=sid)
    page = admin_client.get(f'/admin/purchasing/po/{po_id}').get_data(as_text=True)
    assert f'<option value="{sid}" selected>修正409停用肉行 (停用)</option>' in page


def test_b410_empty_suggestion_text(app):
    # 用獨立的店家：預設店家裡其他測試留下的低庫存原料也會出現在建議清單
    from app.models.tenant import Tenant
    from app.services import settings_service
    from tests.support import other_shop_owner_client
    owner = other_shop_owner_client(app, 'fix-b410-shop')
    with app.app_context():
        tid = Tenant.query.filter_by(slug='fix-b410-shop').one().id
        settings_service.put('inventory', {'enabled': True}, tenant_id=tid)
        milk = Ingredient(tenant_id=tid, name='修正410牛奶', unit='g', stock=0, low_threshold=100, unit_cost=1.0)
        db.session.add(milk)
        db.session.flush()
        po = PurchaseOrder(tenant_id=tid, status='draft', created_by='t')
        po.lines = [PurchaseOrderLine(tenant_id=tid, ingredient_id=milk.id, quantity=1000, unit_cost=1)]
        db.session.add(po)
        db.session.commit()
    page = owner.get('/admin/purchasing').get_data(as_text=True)
    assert '目前沒有低於安全量的原料' not in page
    assert '目前沒有需要補貨的原料' in page


def test_r2_154_member_price_equal_to_add_on_price_is_not_add_on(app):
    from app.models.order import Order
    tea = make_item(app, '修正154紅茶', price=40, category='飲料', member_price=30, can_be_add_on=True, add_on_price=30)
    main = make_item(app, '修正154主餐', price=150)
    make_member(app, '修正154會員', '0977154001', pin='2468')
    c = member_client(app, '0977154001', '2468')
    r = checkout(c, [line(tea)])
    assert r.status_code == 200, r.get_data(as_text=True)
    order_id = r.get_json()['order_id']
    with app.app_context():
        assert db.session.get(Order, order_id).items[0].price == 30
    lines = c.get(f'/api/reorder/{order_id}').get_json()['lines']
    assert [ln['is_add_on'] for ln in lines] == [False]

    # 真正的加購 (搭主餐、加購價低於這筆訂單的正價) 仍還原為加購
    guest = app.test_client()
    guest.get('/?shop=default')
    guest.post('/guest_login')
    r = checkout(guest, [line(main), line(tea, is_add_on=True)])
    assert r.status_code == 200, r.get_data(as_text=True)
    lines = guest.get(f"/api/reorder/{r.get_json()['order_id']}").get_json()['lines']
    assert {ln['menu_item_id']: ln['is_add_on'] for ln in lines} == {main: False, tea: True}


def test_r2_161_forecast_out_of_range_date_is_rejected(admin_client):
    r = admin_client.get('/admin/forecast?date=0001-01-03')
    assert r.status_code in (302, 400)  # AppError：頁面路由會 flash 並轉址，不再是 500
    assert admin_client.get('/admin/forecast?date=2030-01-03').status_code == 200


def test_r2_193_po_line_amount_overflow_does_not_500(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    ing = _ing(app, '修正193麵粉')
    po_id, (line_id,) = _draft(app, [ing], qty=1e300)
    with app.app_context():
        db.session.get(PurchaseOrderLine, line_id).unit_cost = 1e300
        db.session.commit()
    assert admin_client.get(f'/admin/purchasing/po/{po_id}').status_code == 200


def test_b396_ingredient_delete_uses_confirmed_standalone_form(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    ing = _ing(app, '修正396鹽')
    page = admin_client.get('/admin/inventory').get_data(as_text=True)
    assert f'form="del-ing-{ing}"' in page
    assert f'id="del-ing-{ing}"' in page and '確定刪除原料「修正396鹽」' in page
    assert 'name="action" value="delete">刪除</button>' not in page
