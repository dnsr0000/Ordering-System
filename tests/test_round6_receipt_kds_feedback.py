"""
第六輪 (階段 2)：電子收據 (Email / LINE：收據視窗、記住 Email、結帳自動傳 LINE、未開放 / 格式錯誤)、
廚房製作卡 (prep_note + 配方用量、KDS 品項旗標)、顧客評價回覆與公開精選 (通知顧客、追蹤頁顯示回覆、未登入頁好評)。
"""
import os

import pytest

from app.extensions import db
from app.models.order import Order, OrderFeedback
from app.services import mail_service
from tests.support import checkout, guest_client, line, login_staff, make_item, make_member, member_client

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def smtp_box(monkeypatch, store_settings):
    store_settings('smtp', host='smtp.example.com', port=587, sender='shop@example.com')
    box = []

    class FakeSMTP:
        def send_message(self, msg):
            box.append(msg)

        def quit(self):
            pass
    monkeypatch.setattr(mail_service, '_connect', lambda cfg: FakeSMTP())
    return box


@pytest.fixture
def line_box(monkeypatch, store_settings):
    from app.services import line_service
    store_settings('line', messaging_enabled=True, messaging_access_token='tok-r6')
    box = []
    monkeypatch.setattr(line_service, 'push_text', lambda tid, uid, text: box.append((uid, text)) or True)
    return box


# ---------------------------------------------------------------------------
# 電子收據
# ---------------------------------------------------------------------------
def test_email_receipt_from_receipt_modal(app, store_settings, smtp_box):
    item = make_item(app, '收據測試漢堡', price=120)
    store_settings('receipt', email_enabled=False, line_enabled=False)
    make_member(app, '收據會員', '0977800160', pin='2468')
    m = member_client(app, '0977800160', '2468')
    oid = checkout(m, [line(item)]).get_json()['order_id']
    assert m.post(f'/api/my_orders/{oid}/receipt/email', json={'email': 'a@b.co'}).status_code == 404, '未開放'
    assert 'id="receipt-send-box"' not in m.get('/').get_data(as_text=True)
    store_settings('receipt', email_enabled=True, line_enabled=False, footer='謝謝再來')
    html = m.get('/').get_data(as_text=True)
    assert 'id="receipt-send-box"' in html and 'id="receiptRemember"' in html and 'data-action="receipt-email"' in html
    assert m.post(f'/api/my_orders/{oid}/receipt/email', json={'email': 'not-an-email'}).status_code == 400
    r = m.post(f'/api/my_orders/{oid}/receipt/email', json={'email': 'me@example.com', 'remember': True})
    assert r.status_code == 200 and r.get_json()['success'] is True
    assert len(smtp_box) == 1
    msg = smtp_box[0]
    body = msg.get_content()
    assert msg['To'] == 'me@example.com' and f'#{oid}' in msg['Subject']
    assert '收據測試漢堡 x1  $120' in body and '應付總額 $120' in body and '謝謝再來' in body
    with app.app_context():
        from app.models.user import User
        assert User.query.filter_by(phone='0977800160').first().email == 'me@example.com'
    # 記住的 Email 預填在收據視窗；訪客的訂單別人拿不到
    assert 'value="me@example.com"' in m.get('/').get_data(as_text=True)
    other = guest_client(app)
    assert other.post(f'/api/my_orders/{oid}/receipt/email', json={'email': 'x@y.z'}).status_code == 404
    # 訪客自己的訂單可以寄 (不記住)
    g_oid = checkout(other, [line(item)]).get_json()['order_id']
    assert other.post(f'/api/my_orders/{g_oid}/receipt/email', json={'email': 'g@example.com', 'remember': True}).status_code == 200
    assert smtp_box[-1]['To'] == 'g@example.com'
    # 英文頁的成功訊息有翻譯
    m.get('/?lang=en')
    store_settings('menu', languages=['en'])
    body = m.post(f'/api/my_orders/{oid}/receipt/email', json={'email': 'me@example.com'}).get_json()
    assert body['message_i18n'].startswith('Your e-receipt')


def test_line_receipt_manual_and_auto_on_checkout(app, store_settings, line_box):
    item = make_item(app, 'LINE 收據餐', price=50)
    store_settings('receipt', email_enabled=False, line_enabled=True, auto_line=False)
    make_member(app, 'LINE 收據會員', '0977800161', pin='2468', line_user_id='U-receipt-1')
    make_member(app, '無 LINE 會員', '0977800162', pin='2468')
    m = member_client(app, '0977800161', '2468')
    html = m.get('/').get_data(as_text=True)
    assert 'data-action="receipt-line"' in html
    oid = checkout(m, [line(item)]).get_json()['order_id']
    assert line_box == [], '未開自動傳送'
    assert m.post(f'/api/my_orders/{oid}/receipt/line', json={}).status_code == 200
    assert len(line_box) == 1 and line_box[0][0] == 'U-receipt-1' and 'LINE 收據餐 x1' in line_box[0][1]
    # 沒綁 LINE 的會員：409，收據視窗也沒有按鈕
    n = member_client(app, '0977800162', '2468')
    assert 'data-action="receipt-line"' not in n.get('/').get_data(as_text=True)
    oid2 = checkout(n, [line(item)]).get_json()['order_id']
    assert n.post(f'/api/my_orders/{oid2}/receipt/line', json={}).status_code == 409
    # 結帳時自動傳 (POS 綁定會員也會)
    store_settings('receipt', email_enabled=False, line_enabled=True, auto_line=True)
    checkout(m, [line(item)])
    assert len(line_box) == 2
    pos = login_staff(app, 'receipt_pos', 'staff')
    with app.app_context():
        from app.models.user import User
        uid = User.query.filter_by(phone='0977800161').first().id
    from tests.support import pos_checkout
    assert pos_checkout(pos, item, user_id=uid).status_code == 200
    assert len(line_box) == 3
    pos_checkout(pos, item)   # 訪客：不傳
    assert len(line_box) == 3


def test_text_receipt_contains_discounts_invoice_and_track_link(app):
    from app.services import receipt_service
    item = make_item(app, '文字收據餐', price=100)
    c = guest_client(app)
    oid = checkout(c, [line(item, 2)], note='少鹽').get_json()['order_id']
    with app.app_context():
        o = db.session.get(Order, oid)
        text = receipt_service.text_receipt(o)
        assert '文字收據餐 x2  $200' in text and f'單號 #{oid}' in text and '付款方式' in text
        assert receipt_service.normalize_email(' A@B.cc ') == 'A@B.cc'
        with pytest.raises(Exception):
            receipt_service.normalize_email('nope')
        assert receipt_service.send_email(999999, o.tenant_id, 'x@y.z') is False
        assert receipt_service.send_line(oid, o.tenant_id) is False, '訪客沒有 LINE'


# ---------------------------------------------------------------------------
# 廚房製作卡
# ---------------------------------------------------------------------------
def test_kitchen_prep_card(app, kitchen_client, admin_client):
    from app.models.inventory import Ingredient, RecipeLine
    from tests.support import default_tenant_id
    item = make_item(app, '製作卡牛肉麵', price=150, prep_note='1. 牛腱燉 90 分鐘\n2. 麵煮 3 分鐘\n擺盤：蔥花在最上面')
    plain = make_item(app, '製作卡白飯', price=20)
    with app.app_context():
        tid = default_tenant_id(app)
        ing = Ingredient(tenant_id=tid, name='製作卡牛腱', unit='g', stock=1000)
        db.session.add(ing)
        db.session.flush()
        db.session.add(RecipeLine(tenant_id=tid, menu_item_id=item, ingredient_id=ing.id, quantity=120))
        db.session.commit()
    pos = login_staff(app, 'prep_card_pos', 'staff')
    pos.post('/api/pos/checkout', json={'items': [line(item), line(plain)], 'payment_method': 'Cash', 'order_type': '內用',
                                        'cash_received': 170})
    orders = kitchen_client.get('/api/kitchen_orders').get_json()['orders']
    lines = {ln['name']: ln for o in orders for ln in o['items']}
    assert lines['製作卡牛肉麵']['has_card'] is True and lines['製作卡牛肉麵']['menu_item_id'] == item
    assert lines['製作卡白飯']['has_card'] is False
    card = kitchen_client.get(f'/api/kitchen/prep_card/{item}').get_json()
    assert card['name'] == '製作卡牛肉麵' and '牛腱燉 90 分鐘' in card['prep_note']
    assert card['recipe'] == [{'ingredient': '製作卡牛腱', 'quantity': 120.0, 'unit': 'g'}]
    assert kitchen_client.get('/api/kitchen/prep_card/999999').status_code == 404
    assert guest_client(app).get(f'/api/kitchen/prep_card/{item}').status_code in (302, 401, 403)
    html = kitchen_client.get('/kitchen').get_data(as_text=True)
    assert 'id="prepCardModal"' in html
    with open(os.path.join(ROOT, 'static', 'js', 'kitchen.js'), encoding='utf-8') as fh:
        js = fh.read()
    assert 'prep-card' in js and '/api/kitchen/prep_card/' in js and 'has_card' in js
    # 菜單進階設定可填
    r = admin_client.post('/admin/menu_pro', data={'action': 'item', 'item_id': plain, 'prep_note': '  盛飯 150g  ',
                                                   'avail_days': list('1234567'), 'spicy_level': '0'})
    assert r.status_code == 302
    with app.app_context():
        from app.models.menu import MenuItem
        assert db.session.get(MenuItem, plain).prep_note == '盛飯 150g'
    assert kitchen_client.get(f'/api/kitchen/prep_card/{plain}').get_json()['recipe'] == []


# ---------------------------------------------------------------------------
# 評價回覆與公開
# ---------------------------------------------------------------------------
def test_feedback_reply_notifies_customer_and_public_reviews(app, store_settings, line_box, admin_client):
    store_settings('feedback', enabled=True, alert_max_rating=0)
    item = make_item(app, '回覆評價餐', price=80)
    make_member(app, '評價會員', '0977800163', pin='2468', line_user_id='U-fb-1')
    m = member_client(app, '0977800163', '2468')
    body = checkout(m, [line(item)]).get_json()
    oid, uuid = body['order_id'], body['order_uuid']
    with app.app_context():
        db.session.get(Order, oid).status = 'Completed'
        db.session.commit()
    phone = app.test_client()
    assert phone.post(f'/api/track/{uuid}/feedback', json={'rating': 5, 'comment': '牛肉很嫩，下次再來'}).status_code == 200
    with app.app_context():
        fb_id = OrderFeedback.query.filter_by(order_id=oid).first().id
    # 未登入頁還沒有好評區
    anon = app.test_client()
    assert 'id="publicReviews"' not in anon.get('/?shop=default').get_data(as_text=True)
    # 店長回覆並公開
    r = admin_client.post(f'/admin/feedback/{fb_id}/reply', data={'reply': '謝謝您！下次送您一杯紅茶', 'is_public': '1', 'days': '30'})
    assert r.status_code == 302
    with app.app_context():
        fb = db.session.get(OrderFeedback, fb_id)
        assert fb.reply == '謝謝您！下次送您一杯紅茶' and fb.is_public is True and fb.replied_at is not None
        from app.models.user import StaffAuditLog
        assert StaffAuditLog.query.filter_by(action_type='FEEDBACK_REPLY').count() >= 1
    assert len(line_box) == 1 and line_box[0][0] == 'U-fb-1' and '下次送您一杯紅茶' in line_box[0][1]
    # 追蹤頁顯示回覆
    data = phone.get(f'/api/track/{uuid}').get_json()
    assert data['feedback']['reply'] == '謝謝您！下次送您一杯紅茶'
    assert 'id="feedbackReply"' in phone.get(f'/track/{uuid}').get_data(as_text=True)
    # 未登入頁：匿名好評
    html = anon.get('/?shop=default').get_data(as_text=True)
    assert 'id="publicReviews"' in html and '牛肉很嫩' in html and '評＊＊' in html and '評價會員' not in html
    assert '下次送您一杯紅茶' in html and f'#{oid}' not in html.split('id="publicReviews"')[1][:2000]
    page = admin_client.get('/admin/feedback').get_data(as_text=True)
    assert f'id="fb-{fb_id}"' in page and 'feedback-reply-form' in page and '公開' in page
    # 取消公開 + 取消回覆
    admin_client.post(f'/admin/feedback/{fb_id}/reply', data={'reply': '', 'days': '30'})
    with app.app_context():
        fb = db.session.get(OrderFeedback, fb_id)
        assert fb.reply == '' and fb.is_public is False and fb.replied_at is None
    assert 'id="publicReviews"' not in anon.get('/?shop=default').get_data(as_text=True)
    # 低分不會公開顯示 (即使勾公開)
    from app.services import feedback_service
    with app.app_context():
        from tests.support import default_tenant_id
        fb = db.session.get(OrderFeedback, fb_id)
        fb.rating = 2
        fb.is_public = True
        db.session.commit()
        assert feedback_service.public_reviews(default_tenant_id(app)) == []
    # 櫃檯沒有 feedback 權限 → 403
    staff = login_staff(app, 'fb_reply_staff', 'staff')
    assert staff.post(f'/admin/feedback/{fb_id}/reply', data={'reply': 'x'}).status_code == 403
