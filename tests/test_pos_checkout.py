"""POS 櫃檯結帳：查會員、設定 PIN、折扣碼、現金驗證、完整結帳"""
from app.extensions import db
from app.models.order import Order
from app.models.user import StaffAuditLog, User
from tests.support import (
    checkout,
    line,
)


def test_member_lookup(app, staff_client):
    """模糊查詢多筆時回傳候選清單；指定 member_id 精準綁定"""
    assert 'posMemberSelectModal' in staff_client.get('/pos').get_data(as_text=True)
    r = staff_client.post('/api/pos/member_lookup', json={'query': '陳'})
    data = r.get_json()
    assert r.status_code == 200 and data['multiple'] is True and len(data['candidates']) >= 2
    target = data['candidates'][0]['id']
    exact = staff_client.post('/api/pos/member_lookup', json={'member_id': target}).get_json()
    assert exact['multiple'] is False and exact['member']['id'] == target
    assert staff_client.post('/api/pos/member_lookup', json={'query': '不存在的人'}).status_code == 404


def test_set_member_pin(app, staff_client):
    """協助舊會員設定 PIN 後可登入，並寫入 POS_SET_MEMBER_PIN 稽核"""
    with app.app_context():
        uid = User.query.filter_by(phone='0912999888').first().id
    assert staff_client.post('/api/pos/set_member_pin', json={'user_id': uid, 'pin': '12a'}).status_code == 400
    r = staff_client.post('/api/pos/set_member_pin', json={'user_id': uid, 'pin': '6688'})
    assert r.status_code == 200 and r.get_json()['success'] is True
    c = app.test_client()
    c.get('/?shop=default')
    assert c.post('/phone_login', json={'phone': '0912999888', 'pin': '6688'}, headers={'X-Forwarded-For': '10.50.1.2'}).status_code == 200
    with app.app_context():
        audit = StaffAuditLog.query.filter_by(action_type='POS_SET_MEMBER_PIN', target_user_id=uid).first()
        assert audit.staff_username == 'pos_user'


def test_verify_promo(app, staff_client):
    """公開碼的消費門檻、折抵金額；會員專屬券未綁會員時無效"""
    verify = lambda code, subtotal, **kw: staff_client.post('/api/pos/verify_promo', json=dict(promo_code=code, subtotal=subtotal, **kw)).get_json()  # noqa: E731
    assert verify('OPEN20', 60)['valid'] is False
    ok = verify('OPEN20', 150)
    assert ok['valid'] is True and ok['discount'] == 20
    no_member = verify('VIP85', 200)
    assert no_member['valid'] is False and '綁定會員' in no_member['message']


def test_cash_validation(app, staff_client, ctx):
    """現金實收不足、非法付款方式皆回 400"""
    burger = [line(ctx['menu_ids']['招牌和牛堡'])]
    short = checkout(staff_client, burger, url='/api/pos/checkout', cash_received=50)
    assert short.status_code == 400 and '實收現金' in short.get_json()['message']
    assert checkout(staff_client, burger, url='/api/pos/checkout', payment_method='EvilPay').status_code == 400


def test_full_pos_checkout(app, staff_client, ctx):
    """和牛堡 120 + A 套餐 75 - OPEN20 - 25 點 = 150；實收 500 找零 350；贈 3 點；收銀員/進場紀錄/稽核"""
    with app.app_context():
        mei_id = User.query.filter_by(phone='0912999888').first().id
    r = checkout(staff_client, [line(ctx['menu_ids']['招牌和牛堡'], combo_option_id=ctx['combo_a'],
                                     customization='美式脆薯(不辣)、錫蘭紅茶(去冰、無糖)')],
                 url='/api/pos/checkout', user_id=mei_id, table_number='A3桌', promo_code='OPEN20', use_points=25,
                 cash_received=500, note='POS 櫃檯結帳測試', ip='10.50.1.5')
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert (d['total_price'], d['cash_received'], d['cash_change']) == (150, 500, 350)
    assert (d['order_source'], d['cashier'], d['remaining_points']) == ('POS', 'pos_user', 128)
    ctx['pos_order_id'] = d['order_id']
    with app.app_context():
        order = db.session.get(Order, d['order_id'])
        assert order.cashier_username == 'pos_user' and order.cash_change == 350
        assert order.customer_log.user_type == 'POS會員' and '陳小美' in order.customer_log.identifier
        audit = StaffAuditLog.query.filter_by(action_type='POS_CREATE_ORDER', target_order_id=order.id).first()
        assert audit.staff_username == 'pos_user' and audit.points_delta == 3 - 25


def test_weak_pin_is_rejected(app, staff_client):
    """容易被猜中的 PIN (0000、1234、1212、手機末碼) 不可設定"""
    from tests.support import make_member
    uid = make_member(app, '弱密碼會員', '0933771234')
    for pin in ('0000', '1234', '987654', '1212', '771234'):
        r = staff_client.post('/api/pos/set_member_pin', json={'user_id': uid, 'pin': pin})
        assert r.status_code == 400 and '容易被猜中' in r.get_json()['message'], pin
    assert staff_client.post('/api/pos/set_member_pin', json={'user_id': uid, 'pin': '2580'}).status_code == 200
