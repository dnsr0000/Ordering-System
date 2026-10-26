"""第三輪 #1~#3：套餐任選 (群組單選、升級加價)、混合付款 (一筆訂單多種付款方式)、單品作廢 / 改單"""
import pytest

from app.extensions import db, limiter
from app.models.coupon import Coupon  # noqa: F401  (確保模型載入)
from app.models.menu import ComboOption, MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import PaymentRecord, Refund
from app.models.user import User
from tests.support import checkout, default_tenant_id, guest_client, line, login_staff, make_item, make_member, \
    run_together

POS = '/api/pos/checkout'


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def _stock(app, item_id):
    with app.app_context():
        return db.session.get(MenuItem, item_id).stock


def _make_combo(admin_client, app, main, name, choices, price=50, side=None):
    data = {'main_item_id': main, 'name': name, 'additional_price': price, 'choices': choices, 'can_addon': '1'}
    if side:
        data['item1_id'] = side
    r = admin_client.post('/admin/add_combo', data=data)
    assert r.status_code == 302, r.get_data(as_text=True)[:200]
    with app.app_context():
        return ComboOption.query.filter_by(name=name).one().id


def _choice_ids(app, combo_id):
    """{(群組, 餐點名稱): ComboOptionItem id}"""
    with app.app_context():
        combo = db.session.get(ComboOption, combo_id)
        return {(s.choice_group, s.menu_item.name): s.id for s in combo.sides if s.choice_group}


# ===========================================================================
# 1. 套餐任選
# ===========================================================================
def test_combo_choice_groups_price_stock_and_validation(app, admin_client, staff_client):
    burger = make_item(app, '任選堡', price=120, stock=20)
    fries = make_item(app, '任選薯條', price=40, stock=20)
    tea, milk_tea, coffee = (make_item(app, n, price=30, stock=20) for n in ('任選紅茶', '任選奶茶', '任選咖啡'))
    combo = _make_combo(admin_client, app, burger, '任選A餐', '飲料: 任選紅茶, 任選奶茶+15, 任選咖啡+20', side=fries)
    ids = _choice_ids(app, combo)
    assert set(ids) == {('飲料', '任選紅茶'), ('飲料', '任選奶茶'), ('飲料', '任選咖啡')}

    # 菜單資料：固定配餐 + 任選群組
    with app.app_context():
        menu_combo = db.session.get(ComboOption, combo).to_dict()
    assert [s['name'] for s in menu_combo['sides']] == ['任選薯條']
    assert [(o['name'], o['extra_price']) for o in menu_combo['choices'][0]['options']] == [
        ('任選紅茶', 0), ('任選奶茶', 15), ('任選咖啡', 20)]
    assert menu_combo['description'] == '任選薯條 + 飲料任選'

    milk = ids[('飲料', '任選奶茶')]
    r = checkout(staff_client, [line(burger, combo_option_id=combo, combo_choices=[milk])], url=POS, cash_received=500)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['total_price'] == 120 + 50 + 15
    with app.app_context():
        oi = OrderItem.query.filter_by(order_id=body['order_id']).one()
        assert oi.customization.startswith('[套餐: 任選A餐]、飲料:任選奶茶(+15)')
    assert (_stock(app, fries), _stock(app, milk_tea), _stock(app, tea), _stock(app, coffee)) == (19, 19, 20, 20)

    # 沒選 / 選了不屬於這個套餐的選項
    assert '請在「飲料」中選擇一項' in checkout(staff_client, [line(burger, combo_option_id=combo)], url=POS,
                                                cash_received=500).get_json()['message']
    assert checkout(staff_client, [line(burger, combo_option_id=combo, combo_choices=[99999])], url=POS,
                    cash_received=500).status_code == 400
    # 選到已售完的選項
    with app.app_context():
        db.session.get(MenuItem, coffee).is_sold_out = True
        db.session.commit()
    r = checkout(staff_client, [line(burger, combo_option_id=combo, combo_choices=[ids[('飲料', '任選咖啡')]])],
                 url=POS, cash_received=500)
    assert r.status_code == 400 and r.get_json()['sold_out_items'] == ['任選咖啡']
    # 取消：回補當時選的奶茶
    assert staff_client.post(f"/api/orders/{body['order_id']}/status", json={'status': 'Cancelled'}).status_code in (200, 403)
    admin_client.post(f"/api/orders/{body['order_id']}/status", json={'status': 'Cancelled'})
    assert (_stock(app, fries), _stock(app, milk_tea)) == (20, 20)


def test_combo_choice_admin_validation_and_edit(app, admin_client):
    burger = make_item(app, '任選驗證堡', price=100, stock=5)
    make_item(app, '任選驗證茶', price=20, stock=5)
    bad = [('飲料: 任選驗證茶', '至少要有兩個選項'), ('飲料: 任選驗證茶, 不存在的飲料', '不在菜單中'),
           ('沒有冒號的群組', '格式錯誤')]
    for text, msg in bad:
        r = admin_client.post('/admin/add_combo', data={'main_item_id': burger, 'name': f'錯誤{msg}', 'additional_price': 10,
                                                        'choices': text}, follow_redirects=True)
        assert msg in r.get_data(as_text=True), text
    make_item(app, '任選驗證汽水', price=20, stock=5)
    combo = _make_combo(admin_client, app, burger, '任選驗證餐', '飲料：任選驗證茶，任選驗證汽水+5')
    with app.app_context():
        c = db.session.get(ComboOption, combo)
        assert c.choices_text == '飲料: 任選驗證茶, 任選驗證汽水+5'
        assert c.to_dict()['choices'][0]['options'][1]['extra_price'] == 5
    page = admin_client.get('/admin').get_data(as_text=True)
    assert '飲料 2 選 1' in page and 'data-choices="飲料: 任選驗證茶, 任選驗證汽水+5"' in page


# ===========================================================================
# 2. 混合付款
# ===========================================================================
def test_split_payment_records_each_tender_and_refunds_back(app, admin_client, store_settings):
    from app.models.member import GiftCard
    from app.services import giftcard_service, wallet_service
    store_settings('member', wallet_enabled=True)
    store_settings('gift_card', enabled=True)
    store_settings('payments', pos_methods=['Cash', 'Credit Card', 'Line Pay'])
    uid = make_member(app, '混合付款會員', '0968100001')
    item = make_item(app, '混合付款套餐', price=200, stock=20)
    with app.app_context():
        tid = default_tenant_id(app)
        wallet_service.change_balance(tid, uid, 100, 'topup')
        code = giftcard_service.sell(tid, 50, 'Cash', 'admin').code
        db.session.commit()
    pos = login_staff(app, 'split_pos', 'manager')
    tenders = [{'method': 'Cash', 'amount': 100, 'received': 500}, {'method': 'Credit Card', 'amount': 50, 'auth_code': 'A12345'},
               {'method': '儲值金', 'amount': 30}, {'method': '禮物卡', 'amount': 20, 'gift_code': code}]

    # 合計不符：不成立訂單、不扣任何東西
    bad = checkout(pos, [line(item)], url=POS, payment_method='混合付款', user_id=uid, cash_received=500,
                   payment_info={'tenders': tenders[:3]})
    assert bad.status_code == 400 and '合計 $180 與應付 $200' in bad.get_json()['message']
    # 點餐機不能用混合付款
    assert checkout(guest_client(app), [line(item)], payment_method='混合付款',
                    payment_info={'tenders': tenders}).status_code == 400

    r = checkout(pos, [line(item)], url=POS, payment_method='混合付款', user_id=uid, cash_received=500,
                 payment_info={'tenders': tenders}, expected_total=200)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert (body['total_price'], body['payment_method'], body['cash_change']) == (200, '混合付款', 400)
    oid = body['order_id']
    with app.app_context():
        recs = sorted((p.method, p.amount) for p in PaymentRecord.query.filter_by(order_id=oid))
        assert recs == [('Cash', 100), ('Credit Card', 50), ('儲值金', 30), ('禮物卡', 20)]
        assert db.session.get(User, uid).wallet_balance == 70
        assert giftcard_service.balance(GiftCard.query.filter_by(code=code).one()) == 30

    # 部分退款：指定退回刷卡、超過該方式可退金額時拒絕
    assert pos.post(f'/api/pos/orders/{oid}/refund', json={'amount': 60, 'reason': '少一份', 'method': 'Credit Card'}
                    ).status_code == 409
    r = pos.post(f'/api/pos/orders/{oid}/refund', json={'amount': 40, 'reason': '少一份', 'method': 'Credit Card'})
    assert r.status_code == 200 and r.get_json()['status'] == 'manual'
    # 整單作廢：其餘每種方式各退回
    assert pos.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled', 'reason': '客人取消'}).status_code == 200
    with app.app_context():
        refunds = sorted((rf.method, rf.amount) for rf in Refund.query.filter_by(order_id=oid))
        assert refunds == [('Cash', 100), ('Credit Card', 10), ('Credit Card', 40), ('儲值金', 30), ('禮物卡', 20)]
        assert db.session.get(User, uid).wallet_balance == 100
        assert giftcard_service.balance(GiftCard.query.filter_by(code=code).one()) == 50
        assert db.session.get(Order, oid).refunded_amount == 200


def test_split_payment_rejects_bad_tenders(app, store_settings):
    store_settings('payments', pos_methods=['Cash', 'Credit Card'])
    item = make_item(app, '混合付款驗證餐', price=100, stock=5)
    pos = login_staff(app, 'split_pos2', 'staff')
    cases = [([{'method': 'Cash', 'amount': 100}], '2 ~ 5 筆'),
             ([{'method': 'Cash', 'amount': 50}, {'method': 'Cash', 'amount': 50}], '只能有一筆'),
             ([{'method': 'Cash', 'amount': 50}, {'method': '儲值金', 'amount': 50}], '不支援'),
             ([{'method': 'Cash', 'amount': 100}, {'method': 'Credit Card', 'amount': 0}], '大於 0'),
             ([{'method': 'Cash', 'amount': 50}, {'method': 'Credit Card', 'amount': 50}], '授權碼')]
    for tenders, msg in cases:
        r = checkout(pos, [line(item)], url=POS, payment_method='混合付款', payment_info={'tenders': tenders})
        assert r.status_code == 400 and msg in r.get_json()['message'], (tenders, r.get_json())
    assert _stock(app, item) == 5


# ===========================================================================
# 3. 單品作廢 / 改單
# ===========================================================================
def _order_items(app, oid):
    with app.app_context():
        return {i.item_name: (i.quantity, i.voided_qty) for i in OrderItem.query.filter_by(order_id=oid)}


def test_void_item_restocks_refunds_proportionally_and_cancel_does_not_double(app, admin_client):
    burger = make_item(app, '作廢測試堡', price=100, stock=20)
    fries = make_item(app, '作廢測試薯條', price=50, stock=20)
    r = checkout(admin_client, [line(burger, 2), line(fries)], url=POS, cash_received=500,
                 order_discount={'type': 'percent', 'value': 10})
    oid = r.get_json()['order_id']
    assert r.get_json()['total_price'] == 225
    with app.app_context():
        fries_line = OrderItem.query.filter_by(order_id=oid, menu_item_id=fries).one().id
        burger_line = OrderItem.query.filter_by(order_id=oid, menu_item_id=burger).one().id

    r = admin_client.post(f'/api/pos/orders/{oid}/void_item', json={'item_id': fries_line, 'quantity': 1, 'reason': '做錯'})
    assert r.status_code == 200 and r.get_json()['amount'] == 45, r.get_json()
    assert _stock(app, fries) == 20 and _order_items(app, oid)['作廢測試薯條'] == (0, 1)
    with app.app_context():
        o = db.session.get(Order, oid)
        assert (o.refunded_amount, o.total_price) == (45, 225)
        assert PaymentRecord.query.filter_by(order_id=oid, kind='refund').one().amount == -45

    # 超過剩下份數 / 作廢掉最後一份
    assert admin_client.post(f'/api/pos/orders/{oid}/void_item',
                             json={'item_id': burger_line, 'quantity': 3, 'reason': 'x'}).status_code == 409
    assert admin_client.post(f'/api/pos/orders/{oid}/void_item',
                             json={'item_id': burger_line, 'quantity': 2, 'reason': 'x'}).status_code == 409
    assert admin_client.post(f'/api/pos/orders/{oid}/void_item',
                             json={'item_id': burger_line, 'quantity': 1, 'reason': ''}).status_code == 400
    assert admin_client.post(f'/api/pos/orders/{oid}/void_item',
                             json={'item_id': burger_line, 'quantity': 1, 'reason': '客人不要'}).status_code == 200
    assert _stock(app, burger) == 19

    # 廚房看板：顯示作廢份數，剩下的照常
    kds = admin_client.get('/api/kitchen_orders').get_json()['orders']
    card = next(o for o in kds if o['id'] == oid)
    assert {i['name']: (i['quantity'], i['voided']) for i in card['items']} == {'作廢測試堡': (1, 1), '作廢測試薯條': (0, 1)}

    # 之後整單取消：只回補剩下的 1 份堡 (不重複回補已作廢的)
    assert admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'}).status_code == 200
    assert (_stock(app, burger), _stock(app, fries)) == (20, 20)
    with app.app_context():
        assert db.session.get(Order, oid).refunded_amount == 225


def test_void_item_on_unpaid_table_order_lowers_amount_due(app, admin_client):
    from app.models.table import DiningTable
    item = make_item(app, '桌邊改單餐', price=80, stock=20)
    admin_client.post('/admin/tables', data={'action': 'create', 'names': 'VOID1'})
    with app.app_context():
        table = DiningTable.query.filter_by(name='VOID1').one()
        token, table_id = table.token, table.id
    phone = app.test_client()
    phone.get(f'/t/{token}')
    phone.post('/guest_login')
    oid = checkout(phone, [line(item, 3)]).get_json()['order_id']
    with app.app_context():
        lid = OrderItem.query.filter_by(order_id=oid).one().id
    r = admin_client.post(f'/api/pos/orders/{oid}/void_item', json={'item_id': lid, 'quantity': 1, 'reason': '點錯'})
    assert r.status_code == 200 and r.get_json()['order_total'] == 160
    settle = admin_client.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash', 'cash_received': 200})
    assert settle.get_json()['total'] == 160


def test_concurrent_voids_never_exceed_quantity(app, admin_client, no_rate_limit):
    item = make_item(app, '同時作廢餐', price=60, stock=20)
    other = make_item(app, '同時作廢配菜', price=40, stock=20)
    oid = checkout(admin_client, [line(item, 2), line(other)], url=POS, cash_received=500).get_json()['order_id']
    with app.app_context():
        lid = OrderItem.query.filter_by(order_id=oid, menu_item_id=item).one().id
    login_staff(app, 'void_mgr', 'manager')

    def void():
        return login_staff(app, 'void_mgr').post(f'/api/pos/orders/{oid}/void_item',
                                                 json={'item_id': lid, 'quantity': 1, 'reason': '同時'})

    results = run_together(*[void] * 4)
    assert sorted(r.status_code for r in results) == [200, 200, 409, 409]
    assert _order_items(app, oid)['同時作廢餐'] == (0, 2) and _stock(app, item) == 20
    with app.app_context():
        assert db.session.get(Order, oid).refunded_amount == 120


def test_void_item_on_split_paid_order_refunds_chosen_tender(app, admin_client, store_settings):
    store_settings('payments', pos_methods=['Cash', 'Credit Card'])
    item = make_item(app, '混合作廢餐', price=100, stock=20)
    other = make_item(app, '混合作廢湯', price=100, stock=20)
    oid = checkout(admin_client, [line(item), line(other)], url=POS, payment_method='混合付款',
                   payment_info={'tenders': [{'method': 'Cash', 'amount': 50},
                                             {'method': 'Credit Card', 'amount': 150, 'auth_code': 'B7788'}]}
                   ).get_json()['order_id']
    with app.app_context():
        lid = OrderItem.query.filter_by(order_id=oid, menu_item_id=item).one().id
    r = admin_client.post(f'/api/pos/orders/{oid}/void_item',
                          json={'item_id': lid, 'quantity': 1, 'reason': '退', 'method': 'Credit Card'})
    assert r.status_code == 200 and r.get_json()['refund_status'] == 'manual'
    with app.app_context():
        assert Refund.query.filter_by(order_id=oid).one().method == 'Credit Card'


# ===========================================================================
# 線上端 ↔ 分店：任選套餐與作廢同步
# ===========================================================================
def test_combo_choices_and_item_void_sync_between_hub_and_branch():
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _order, tenant_ctx
    env = Env()
    try:
        burger = _add_item(env.b, '同步任選堡', price=120, stock=20)
        tea = _add_item(env.b, '同步任選茶', price=30, stock=20)
        cola = _add_item(env.b, '同步任選可樂', price=30, stock=20)
        env.sync_all()
        with tenant_ctx(env.hub) as tenant:
            from app.models.menu import ComboOptionItem
            main = MenuItem.query.filter_by(uuid=burger).one()
            combo = ComboOption(tenant_id=tenant.id, main_item_id=main.id, name='同步任選餐', additional_price=40)
            db.session.add(combo)
            db.session.flush()
            for n, u in enumerate((tea, cola)):
                db.session.add(ComboOptionItem(tenant_id=tenant.id, combo_option_id=combo.id, position=n,
                                               menu_item_id=MenuItem.query.filter_by(uuid=u).one().id,
                                               choice_group='飲料', extra_price=5 * n))
            db.session.commit()
            combo_uuid = combo.uuid
        env.sync_all()
        with tenant_ctx(env.b):
            c = ComboOption.query.filter_by(uuid=combo_uuid).one()
            assert c.choices_text == '飲料: 同步任選茶, 同步任選可樂+5'
            cola_choice = next(s.id for s in c.sides if s.menu_item.uuid == cola)
            combo_b = c.id
        order = _checkout(env.b, [line(_item(env.b, burger)['id'], 2, combo_option_id=combo_b,
                                       combo_choices=[cola_choice])],
                          order_source='POS', cashier_username='b_pos', cash_received=1000)
        assert order['total'] == (120 + 40 + 5) * 2
        assert _item(env.b, cola)['stock'] == 18 and _item(env.b, tea)['stock'] == 20
        from app.services.order_service import void_item
        with tenant_ctx(env.b) as tenant:
            oi = OrderItem.query.join(Order).filter(Order.uuid == order['uuid']).one()
            void_item(tenant.id, oi.id, 1, '同步測試')
            db.session.commit()
        assert _item(env.b, cola)['stock'] == 19 and _item(env.b, burger)['stock'] == 19
        env.sync(env.b)
        with tenant_ctx(env.hub):
            oi = OrderItem.query.join(Order).filter(Order.uuid == order['uuid']).one()
            assert (oi.quantity, oi.voided_qty) == (1, 1)
            assert '飲料:同步任選可樂(+5)' in oi.customization
        assert _order(env.hub, order['uuid'])['status'] == 'Pending'
    finally:
        env.close()


def test_sequential_voids_on_paid_order_refund_each_share_once(app, admin_client, no_rate_limit):
    """已收款訂單連續作廢兩次：第二次依「剩下的小計與尚未退的金額」計算，合計不會多退 (曾經第二次退 96 而非 60)"""
    item = make_item(app, '連續作廢餐', price=60, stock=20)
    other = make_item(app, '連續作廢配菜', price=40, stock=20)
    oid = checkout(admin_client, [line(item, 2), line(other)], url=POS, cash_received=500).get_json()['order_id']
    with app.app_context():
        lid = OrderItem.query.filter_by(order_id=oid, menu_item_id=item).one().id
    amounts = [admin_client.post(f'/api/pos/orders/{oid}/void_item',
                                 json={'item_id': lid, 'quantity': 1, 'reason': '連續'}).get_json()['amount']
               for _ in range(2)]
    assert amounts == [60, 60]
    with app.app_context():
        order = db.session.get(Order, oid)
        assert order.refunded_amount == 120 and order.refundable_amount == 40
