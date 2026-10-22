"""自助點餐結帳：伺服器端計價、套餐加料與庫存、白名單、取餐號、四捨五入、重複送出冪等、訪客訂單範圍、折扣碼驗證"""
from datetime import date

from app.extensions import db
from app.models.coupon import Coupon
from app.models.menu import MenuItem, ModifierOption
from app.models.order import DailyCounter, Order, OrderItem
from tests.support import (
    PHOTO_MEMBER_PHONE,
    PHOTO_MEMBER_PIN,
    checkout,
    default_tenant_id,
    guest_client,
    line,
    make_item,
    make_member,
    member_client,
    new_client_order_id,
    pos_checkout,
    run_together,
)


def test_server_side_pricing_and_customization_cleanup(app, ctx):
    """價格一律後端查表 (忽略前端 price)、加料依 modifier_ids 計價、偽造的價格標籤被清除"""
    burger = ctx['menu_ids']['招牌和牛堡']
    with app.app_context():
        mods = ModifierOption.query.filter_by(is_active=True).order_by(ModifierOption.id).limit(2).all()
        mod_ids, mod_sum = [m.id for m in mods], sum(m.price for m in mods)
    r = checkout(guest_client(app), [line(burger, 2, price=1, modifier_ids=mod_ids,
                                          customization='加荷包蛋(+0)、不要洋蔥(+999)')],
                 order_type='外帶', need_cutlery=True, note='訪客單點測試', ip='10.30.1.1')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['total_price'] == (120 + mod_sum) * 2
    custom = data['items'][0]['customization']
    assert '(+0)' not in custom and '(+999)' not in custom and '不要洋蔥' in custom


def test_combo_addon_and_stock(app, ctx):
    """套餐 (結構化 combo_option_id) + 配餐客製化 + 加購品：計價與主餐/配餐/加購品庫存同步扣減"""
    ids = ctx['menu_ids']
    watched = ['招牌和牛堡', '美式脆薯', '錫蘭紅茶', '松露沾醬']
    with app.app_context():
        before = {n: db.session.get(MenuItem, ids[n]).stock for n in watched}
    r = checkout(guest_client(app), [
        line(ids['招牌和牛堡'], combo_option_id=ctx['combo_a'], customization='美式脆薯(小辣)、錫蘭紅茶(少冰、半糖)'),
        line(ids['松露沾醬'], is_add_on=True),
    ], payment_method='Line Pay', ip='10.30.1.2')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['total_price'] == 120 + 75 + 20
    assert data['items'][0]['customization'].startswith('[套餐: A套餐(薯條+紅茶)]')
    with app.app_context():
        after = {n: db.session.get(MenuItem, ids[n]).stock for n in watched}
    assert all(after[n] == before[n] - 1 for n in watched), (before, after)


def test_addon_quota_matching(app, ctx):
    """加購配額 (二分圖配對)：超量、無合格主餐、不符單品條件、總量夠但無法一對一配對皆擋下"""
    ids = ctx['menu_ids']
    c = guest_client(app)

    def post(*lines, ip):
        return checkout(c, list(lines), payment_method='Line Pay', ip=ip).status_code

    assert post(line(ids['招牌和牛堡']), line(ids['松露沾醬'], 2, is_add_on=True), ip='10.31.1.41') == 400
    assert post(line(ids['松露沾醬'], is_add_on=True), ip='10.31.1.42') == 400
    assert post(line(ids['美式脆薯']), line(ids['松露沾醬'], is_add_on=True), ip='10.31.1.43') == 400
    assert post(line(ids['招牌和牛堡']), line(ids['美式脆薯']), line(ids['錫蘭紅茶'], is_add_on=True),
                line(ids['松露沾醬'], is_add_on=True), ip='10.31.1.44') == 400
    assert post(line(ids['招牌和牛堡'], 2), line(ids['錫蘭紅茶'], is_add_on=True),
                line(ids['松露沾醬'], is_add_on=True), ip='10.31.1.45') == 200


def test_whitelists_and_invalid_combo(app, ctx):
    """付款方式 / 用餐方式白名單，以及不屬於該餐點的套餐選項"""
    ids = ctx['menu_ids']
    c = guest_client(app)
    tea = [line(ids['錫蘭紅茶'])]
    assert checkout(c, tea, payment_method='Bitcoin_Hack').status_code == 400
    assert checkout(c, tea, order_type='無人機空投').status_code == 400
    assert checkout(c, [line(ids['錫蘭紅茶'], combo_option_id=ctx['combo_a'])]).status_code == 400
    assert checkout(c, [{'name': '不存在', 'quantity': 1}]).status_code == 400
    # 舊版前端的付款別名仍可使用，存檔時轉為標準值
    r = checkout(c, tea, payment_method='LinePay')
    assert r.status_code == 200 and r.get_json()['payment_method'] == 'Line Pay'


def test_pickup_number_wraps_after_999(app):
    """取餐號 1~999 循環：達 999 後回繞為 1 並持續遞增"""
    item_id = make_item(app, '取餐號回繞測試餐', price=20, stock=10)
    with app.app_context():
        counter = db.session.get(DailyCounter, (default_tenant_id(app), date.today(), 'pickup'))
        counter.value = 999
        db.session.commit()
    seq = [checkout(guest_client(app), [line(item_id)]).get_json()['pickup_number'] for _ in range(3)]
    assert seq == [1, 2, 3]


def test_customer_verify_promo(app):
    tid = default_tenant_id(app)
    with app.app_context():
        db.session.add_all([
            Coupon(tenant_id=tid, code='X13PUB', title='模組13公開券', discount_type='fixed', discount_value=10, min_spend=100,
                   reward_points=0, is_reward=False),
            Coupon(tenant_id=tid, code='X13RWD', title='模組13紅利券', discount_type='fixed', discount_value=50, min_spend=0,
                   reward_points=50, is_reward=True)])
        db.session.commit()
    c = app.test_client()
    c.get('/?shop=default')
    verify = lambda code, subtotal: c.post('/api/verify_promo', json={'promo_code': code, 'subtotal': subtotal}).get_json()  # noqa: E731
    assert verify('', 200)['valid'] is False
    assert verify('NO_SUCH_CODE', 200)['valid'] is False
    low = verify('X13PUB', 50)
    assert low['valid'] is False and '門檻' in low['message']
    ok = verify('x13pub', 200)
    assert ok['valid'] is True and ok['discount'] == 10
    rwd = verify('X13RWD', 200)
    assert rwd['valid'] is False and '登入' in rwd['message']


def test_my_orders(app, photo_member):
    tid = default_tenant_id(app)
    anon = app.test_client()
    anon.get('/?shop=default')
    assert anon.get('/my_orders').get_json() == []
    other = make_member(app, '他人13', '0933130014')
    with app.app_context():
        db.session.add_all([
            Order(tenant_id=tid, user_id=photo_member, table_number='模組13會員', total_price=100, status='Pending', pickup_number=1),
            Order(tenant_id=tid, user_id=photo_member, table_number='模組13會員', total_price=200, status='Completed', pickup_number=2),
            Order(tenant_id=tid, user_id=other, table_number='他人', total_price=999, status='Pending', pickup_number=3)])
        db.session.commit()
    data = member_client(app, PHOTO_MEMBER_PHONE, PHOTO_MEMBER_PIN).get('/my_orders').get_json()
    assert [d['total_price'] for d in data] == [200, 100]
    assert [d['user_order_no'] for d in data] == [2, 1]
    assert [d['status'] for d in data] == ['已出餐', '製作中']


def test_rounding_keeps_totals_consistent(app, admin_client):
    """105 元 9 折 (折 10.5)：應付四捨五入為 95，且 小計 = 應付 + 折扣"""
    tid = default_tenant_id(app)
    iid = make_item(app, '模組15金額品', price=105, stock=50)
    with app.app_context():
        db.session.add(Coupon(tenant_id=tid, code='M15P', title='M15九折', discount_type='percent', discount_value=90,
                              min_spend=0, reward_points=0, is_reward=False))
        db.session.commit()
    d = pos_checkout(admin_client, iid, promo='M15P').get_json()
    assert (d['subtotal'], d['total_price'], d['discount_amount']) == (105, 95, 10)


def test_client_order_id_makes_checkout_idempotent(app):
    """同一個 client_order_id 同時送出兩次只建立一張訂單；換新的 id 則是新訂單"""
    iid = make_item(app, '模組15冪等品', price=20, stock=50)
    c = guest_client(app)
    order_uuid = new_client_order_id()
    a, b = run_together(lambda: checkout(c, [line(iid)], client_order_id=order_uuid),
                        lambda: checkout(c, [line(iid)], client_order_id=order_uuid))
    assert a.status_code == b.status_code == 200
    assert a.get_json()['order_id'] == b.get_json()['order_id']
    assert {a.get_json()['replayed'], b.get_json()['replayed']} == {True, False}
    with app.app_context():
        assert OrderItem.query.filter_by(menu_item_id=iid).count() == 1
        assert db.session.get(MenuItem, iid).stock == 49
    r3 = checkout(c, [line(iid)], client_order_id=new_client_order_id())
    assert r3.get_json()['order_id'] != a.get_json()['order_id']


def test_guest_orders_scoped_to_visit(app):
    tid = default_tenant_id(app)
    c = guest_client(app)
    with c.session_transaction() as sess:
        name, log_id = sess['user_name'], sess['customer_log_id']
    with app.app_context():
        db.session.add_all([Order(tenant_id=tid, table_number=name, total_price=111, status='Pending', pickup_number=5),
                            Order(tenant_id=tid, table_number=name, customer_log_id=log_id, total_price=222, status='Pending', pickup_number=6)])
        db.session.commit()
    assert [o['total_price'] for o in c.get('/my_orders').get_json()] == [222]


def test_pickup_counter_continues_from_existing_orders(app):
    """當天第一次取號 (例如升級當天) 會接續今天已存在訂單的最後號碼"""
    tid = default_tenant_id(app)
    with app.app_context():
        last = db.session.execute(db.select(Order.pickup_number).where(Order.tenant_id == tid)
                                  .order_by(Order.id.desc()).limit(1)).scalar()
        db.session.delete(db.session.get(DailyCounter, (tid, date.today(), 'pickup')))
        db.session.commit()
    iid = make_item(app, '模組15取號品', price=10, stock=5)
    r = checkout(guest_client(app), [line(iid)]).get_json()
    assert r['pickup_number'] == last % 999 + 1


def test_replay_only_returns_order_to_its_owner(app, ctx):
    """拿到別人的 client_order_id 重送：不可取得別人的收據 (會員姓名、點數、品項)，原下單者重送則回傳同一張"""
    order_id = new_client_order_id()
    first = guest_client(app)
    r = checkout(first, [line(ctx['menu_ids']['錫蘭紅茶'])], client_order_id=order_id)
    assert r.status_code == 200
    other = guest_client(app)
    assert checkout(other, [line(ctx['menu_ids']['錫蘭紅茶'])], client_order_id=order_id).status_code == 409
    again = checkout(first, [line(ctx['menu_ids']['錫蘭紅茶'])], client_order_id=order_id)
    assert again.status_code == 200 and again.get_json()['order_id'] == r.get_json()['order_id']
