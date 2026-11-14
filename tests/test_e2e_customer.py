"""端對端：顧客自助點餐 (Kiosk) 的實際操作流程 — 訪客/會員點餐、客製化、套餐、優惠券、XSS 顯示"""
import re

import pytest
from playwright.sync_api import expect

from tests.support import browser_member_login, seed_demo
from tests.support import default_tenant_id as demo_tenant_id

pytestmark = pytest.mark.e2e

XSS_ITEM = "O'Brien 的「特製」<script>alert(1)</script>"
COMBO_NAME = "A套餐(薯條+紅茶) <i>'含飲料'</i>"
MEMBER_NAME = "示範會員 </script><b>王'小明'"
MEMBER_PHONE = '0911222333'


@pytest.fixture
def demo(app):
    """示範資料 + 把本測試用到的品項庫存補滿 (e2e 測試共用資料庫，其他測試可能已買走部分庫存)"""
    from app.extensions import db
    from app.models.menu import ComboOption, MenuItem
    ids = seed_demo(app)
    with app.app_context():
        for name in ('示範牛肉堡', '示範紅茶', '示範薯條', '示範雞塊', XSS_ITEM):
            item = db.session.get(MenuItem, ids[name])
            item.stock, item.total_stock, item.is_sold_out = 50, max(item.total_stock or 0, 50), False
        db.session.commit()
        combo_id = ComboOption.query.filter_by(main_item_id=ids['示範牛肉堡'], name=COMBO_NAME).first().id
    return dict(ids, combo_id=combo_id)


def order_by_uuid(app, client_order_id):
    from app.models.order import Order
    with app.app_context():
        order = Order.query.filter_by(client_order_id=client_order_id).first()
        assert order is not None, f'資料庫找不到 client_order_id={client_order_id} 的訂單'
        return {
            'total_price': order.total_price,
            'discount_amount': order.discount_amount,
            'coupon_code': order.coupon_code,
            'user_name': order.user.name if order.user else None,
            'items': sorted(
                [(i.item_name, i.quantity, i.price, i.combo_option_id, i.customization) for i in order.items],
                key=lambda t: t[0]),
        }


def add_from_menu(page, item_id):
    page.locator(f'#menuList .menu-row[data-id="{item_id}"] [data-action="add"]').click()


def open_cart(page):
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()


def checkout(page):
    """開啟確認視窗並送出訂單，回傳送出的 JSON 內容"""
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    with page.expect_request(lambda r: r.url.endswith('/submit_order') and r.method == 'POST') as req:
        page.locator('[data-action="checkout"]').click()
    page.locator('#receiptModal.show').wait_for()
    return req.value.post_data_json


def test_guest_orders_plain_and_customized_items(app, page, demo):
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')

    # XSS 品名在菜單中顯示為純文字 (不會被當成標籤)
    menu_name = page.locator(f'#menuList .menu-row[data-id="{demo[XSS_ITEM]}"] .menu-name')
    assert XSS_ITEM in menu_name.inner_text()
    assert page.locator('#menuList script').count() == 0
    # 商品描述中的 <b> 顯示為文字
    expect_text = page.get_by_text('多汁牛肉 <b>不是粗體</b>', exact=True)
    assert expect_text.first.is_visible()
    assert page.locator('#menuList b').count() == 0

    # 一般商品：直接加入購物車
    add_from_menu(page, demo[XSS_ITEM])
    # 客製化商品：冰塊/甜度
    add_from_menu(page, demo['示範紅茶'])
    page.locator('#modifierModal.show').wait_for()
    page.select_option('#modifierModalBody [data-mod="ice"]', '去冰')
    page.select_option('#modifierModalBody [data-mod="sugar"]', '微糖')
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('#modifierModal').wait_for(state='hidden')
    assert page.locator('#cart-count').inner_text() == '2'

    open_cart(page)
    cart_names = page.locator('#cart-items .cart-line-name').all_inner_texts()
    assert XSS_ITEM in cart_names and '示範紅茶' in cart_names
    assert page.locator('#cart-items').get_by_text('去冰/微糖').is_visible()
    assert page.locator('#cart-final').inner_text() == '139'

    payload = checkout(page)
    assert payload['client_order_id']
    assert {i['menu_item_id'] for i in payload['items']} == {demo[XSS_ITEM], demo['示範紅茶']}
    assert page.locator('#receipt-total').inner_text() == '139'
    assert page.locator('#receipt-items-body').get_by_text(XSS_ITEM, exact=True).is_visible()

    order = order_by_uuid(app, payload['client_order_id'])
    assert order['total_price'] == 139 and order['user_name'] is None
    assert (XSS_ITEM, 1, 99, None, '') in order['items']
    assert ('示範紅茶', 1, 40, None, '去冰/微糖') in order['items']

    # 繼續點餐：購物車清空
    page.locator('[data-action="finish"]').click()
    page.wait_for_load_state('networkidle')
    assert page.locator('#cart-count').inner_text() == '0'


def test_member_applies_coupon_e2e20(app, page, demo):
    from app.models.user import RewardSetting, User
    browser_member_login(page)
    page.goto('/')
    page.wait_for_load_state('networkidle')
    # 會員名稱含 HTML 特殊字元，顯示為純文字
    assert page.locator('#member-name').inner_text() == MEMBER_NAME
    with app.app_context():
        points_before = User.query.filter_by(phone=MEMBER_PHONE).first().points
        setting = RewardSetting.query.filter_by(tenant_id=demo_tenant_id(app)).first()
        earned = 100 // max(1, setting.spend_per_point or 100) if setting else 1
    assert page.locator('#nav-user-points').inner_text() == str(points_before)

    # 牛肉堡有套餐：選擇單點，再於客製化視窗直接確認 (不加料)
    add_from_menu(page, demo['示範牛肉堡'])
    page.locator('#comboModal.show').wait_for()
    assert page.locator('#combo-cards-container .combo-name').first.inner_text() == COMBO_NAME
    page.locator('#combo-cards-container [data-action="pick-combo"][data-combo-id=""]').click()
    page.locator('#modifierModal.show').wait_for()
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('#modifierModal').wait_for(state='hidden')

    open_cart(page)
    assert page.locator('#cart-subtotal').inner_text() == '120'
    page.fill('#promoInput', 'e2e20')
    page.locator('[data-action="apply-promo"]').click()
    page.locator('#promo-msg.text-success').wait_for()
    assert page.locator('#cart-promo-discount').inner_text() == '20'
    assert page.locator('#cart-final').inner_text() == '100'
    assert page.locator('#cart-earned-points').inner_text() == str(earned)

    payload = checkout(page)
    assert payload['promo_code'] == 'e2e20' and payload['use_points'] == 0
    assert page.locator('#receipt-total').inner_text() == '100'
    assert page.locator('#receipt-discount').inner_text() == '20'
    assert page.locator('#receipt-user').inner_text() == MEMBER_NAME

    order = order_by_uuid(app, payload['client_order_id'])
    assert order['total_price'] == 100 and order['discount_amount'] == 20
    assert order['coupon_code'] == 'E2E20' and order['user_name'] == MEMBER_NAME
    assert [(n, q, p, c) for n, q, p, c, _ in order['items']] == [('示範牛肉堡', 1, 120, None)]
    assert page.locator('#receipt-current-points').inner_text() == f'{points_before + earned} 點'

    # 我的訂單：可看到剛剛的訂單 (品名顯示為純文字)
    page.locator('[data-action="finish"]').click()
    page.wait_for_load_state('networkidle')
    page.locator('[data-action="my-orders"]').click()
    page.locator('#historyModal.show').wait_for()
    page.locator('#history-modal-body .card').first.wait_for()
    assert '實付：$100' in page.locator('#history-modal-body .card').first.inner_text()


@pytest.mark.parametrize('send_server_notification', [True, False], ids=['server-notify', 'browser-return-fallback'])
def test_expired_online_order_can_reorder_and_complete_payment(app, page, demo, monkeypatch, live_url,
                                                               send_server_notification):
    from datetime import datetime
    from uuid import uuid4

    from app.extensions import db
    from app.models.enums import OrderStatus
    from app.models.menu import MenuItem
    from app.models.order import Order
    from app.models.sync import NodeStock, SyncNode
    from tests.test_sync_hub_node import tenant_ctx

    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    monkeypatch.setitem(app.config, 'ONLINE_PAYMENT', True)
    previous_nodes = []
    node_id = node_uuid = None
    previous_price = None
    item_id = demo[XSS_ITEM]
    try:
        with tenant_ctx(app) as tenant:
            previous_nodes = [(node.id, node.is_active) for node in SyncNode.query.filter_by(tenant_id=tenant.id).all()]
            for existing in SyncNode.query.filter_by(tenant_id=tenant.id).all():
                existing.is_active = False
            node = SyncNode(tenant_id=tenant.id, name=f'E2E 逾時取餐店 {uuid4().hex[:8]}',
                            token_hash=uuid4().hex * 2, last_seen_at=datetime.now())
            db.session.add(node)
            db.session.flush()
            node_id, node_uuid = node.id, node.uuid
            db.session.add(NodeStock(node_id=node.id, menu_item_id=item_id, stock=50))
            db.session.commit()

        client = app.test_client()
        client.get('/?shop=default')
        login = client.post('/phone_login', json={'phone': MEMBER_PHONE, 'pin': '2468'},
                            headers={'X-Forwarded-For': '10.77.0.1'})
        assert login.status_code == 200, login.get_data(as_text=True)
        assert client.post('/pickup_node', data={'node_uuid': node_uuid}).status_code == 302
        old_response = client.post('/submit_order', json={
            'items': [{'menu_item_id': item_id, 'quantity': 1}], 'payment_method': 'Credit Card', 'order_type': '內用',
        })
        assert old_response.status_code == 200, old_response.get_data(as_text=True)
        old_order_uuid = old_response.get_json()['order_uuid']

        with tenant_ctx(app):
            old_order = Order.query.filter_by(uuid=old_order_uuid).first()
            assert old_order.payment_status == 'pending' and old_order.total_price == 99
            old_order_id = old_order.id
            old_order.payment_status = 'expired'
            old_order.status = OrderStatus.CANCELLED
            item = db.session.get(MenuItem, item_id)
            previous_price = item.price
            item.price = 129
            db.session.commit()

        browser_member_login(page)
        page.goto('/?shop=default')
        page.wait_for_load_state('networkidle')
        page.locator('[data-action="my-orders"]').click()
        page.locator('#historyModal.show').wait_for()
        expired_card = page.locator('#history-modal-body .card').filter(
            has=page.locator(f'.order-reorder[data-id="{old_order_id}"]'))
        expect(expired_card).to_contain_text('原訂單已取消')
        expect(expired_card).to_contain_text('目前菜單價格')
        expect(expired_card.locator('.order-reorder')).to_contain_text('重新下單')
        expect(expired_card.locator('.order-pay')).to_have_count(0)
        expired_card.locator('.order-reorder').click()
        page.locator('#historyModal').wait_for(state='hidden')
        expect(page.locator('#cart-count')).to_have_text('1')

        open_cart(page)
        expect(page.locator('#cart-items').get_by_text(XSS_ITEM, exact=True)).to_be_visible()
        expect(page.locator('#cart-final')).to_have_text('129')

        submitted_client_order_ids = []

        def capture_checkout_request(request):
            if request.url.endswith('/submit_order') and request.method == 'POST':
                submitted_client_order_ids.append(request.post_data_json['client_order_id'])

        def intercept_payment_page(route):
            route.fulfill(status=200, content_type='text/html', body='<p id="payment-navigation-caught">Payment intercepted</p>')

        page.on('request', capture_checkout_request)
        page.route('**/pay/**', intercept_payment_page)
        page.route('https://payment-stage.ecpay.com.tw/**', intercept_payment_page)
        page.locator('[data-action="preview"]').click()
        page.locator('#confirmModal.show').wait_for()
        page.locator('[data-action="checkout"]').click(no_wait_after=True)
        expect(page.locator('#payment-navigation-caught')).to_be_visible()
        assert submitted_client_order_ids

        with tenant_ctx(app):
            new_order = Order.query.filter_by(client_order_id=submitted_client_order_ids[-1]).first()
            assert new_order.payment_status == 'pending' and new_order.total_price == 129
            assert new_order.uuid != old_order_uuid
            new_order_id, new_order_uuid, payment_ref = new_order.id, new_order.uuid, new_order.payment_ref
        mine = next(order for order in page.request.get('/my_orders').json() if order['order_id'] == new_order_id)
        assert mine['payment_url'].endswith('/pay/' + new_order_uuid)
        payment_page = page.request.get(mine['payment_url'])
        assert payment_page.status == 200
        assert 'https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5' in payment_page.text()

        from tests.test_payment_ecpay import _notify_params, _signed
        notification = _signed(app, _notify_params(
            {'uuid': new_order_uuid, 'total': 129}, MerchantTradeNo=payment_ref, TradeNo=f'E2E{uuid4().hex[:16]}'))
        if send_server_notification:
            notify_response = page.request.post('/payment/ecpay/notify', form=notification)
            assert notify_response.status == 200 and notify_response.text() == '1|OK'
            page.goto('/payment/return/' + new_order_uuid)
        else:
            from html import escape

            result_inputs = ''.join(
                f'<input type="hidden" name="{escape(str(name), quote=True)}" value="{escape(str(value), quote=True)}">'
                for name, value in notification.items())
            result_action = escape(f'{live_url}/payment/ecpay/result', quote=True)
            gateway_html = ('<!doctype html><meta charset="utf-8">'
                            f'<form id="ecpay-result" method="post" action="{result_action}">{result_inputs}</form>'
                            '<script>document.getElementById("ecpay-result").submit()</script>')
            browser_return_headers = []
            server_notifications = []

            def capture_payment_requests(request):
                if request.url.endswith('/payment/ecpay/result'):
                    browser_return_headers.append(request.headers)
                elif request.url.endswith('/payment/ecpay/notify'):
                    server_notifications.append(request.url)

            page.on('request', capture_payment_requests)
            page.route('https://payment-stage.ecpay.com.tw/**',
                       lambda route: route.fulfill(status=200, content_type='text/html', body=gateway_html))
            page.goto('https://payment-stage.ecpay.com.tw/browser-return')
            expect(page.locator('#payment-result')).to_have_attribute('data-status', 'paid')
            assert browser_return_headers and 'cookie' not in browser_return_headers[-1]
            assert server_notifications == []

        paid_order = next(order for order in page.request.get('/my_orders').json()
                          if order['order_id'] == new_order_id)
        assert paid_order['payment_status'] == 'paid' and paid_order['payment_url'] == ''
        expect(page.locator('#payment-result')).to_have_attribute('data-status', 'paid')
        from app.models.payment import PaymentKind, PaymentRecord
        with tenant_ctx(app):
            sales = PaymentRecord.query.filter_by(order_id=new_order_id, kind=PaymentKind.SALE).all()
            assert len(sales) == 1
    finally:
        with tenant_ctx(app):
            db.session.rollback()
            if node_id is not None:
                NodeStock.query.filter_by(node_id=node_id).delete(synchronize_session=False)
                test_node = db.session.get(SyncNode, node_id)
                if test_node:
                    db.session.delete(test_node)
            for existing_id, was_active in previous_nodes:
                existing = db.session.get(SyncNode, existing_id)
                if existing:
                    existing.is_active = was_active
            if previous_price is not None:
                db.session.get(MenuItem, item_id).price = previous_price
            db.session.commit()


def test_combo_order_with_side_customization(app, page, demo):
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')

    add_from_menu(page, demo['示範牛肉堡'])
    page.locator('#comboModal.show').wait_for()
    # 套餐名稱含 HTML，顯示為純文字
    assert page.locator('#comboModal').get_by_text(COMBO_NAME, exact=True).is_visible()
    assert page.locator('#comboModal i:has-text("含飲料")').count() == 0
    page.locator(f'#combo-cards-container [data-combo-id="{demo["combo_id"]}"]').click()

    page.locator('#modifierModal.show').wait_for()
    body = page.locator('#modifierModalBody')
    assert body.get_by_text('配餐：示範薯條').is_visible() and body.get_by_text('配餐：示範紅茶').is_visible()
    page.select_option('.mod-section[data-key="side0"] [data-mod="spicy"]', '中辣')
    page.select_option('.mod-section[data-key="side1"] [data-mod="ice"]', '少冰')
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('#modifierModal').wait_for(state='hidden')

    open_cart(page)
    assert page.locator('#cart-items').get_by_text(f'[套餐: {COMBO_NAME}]', exact=False).is_visible()
    assert page.locator('#cart-final').inner_text() == '180'

    payload = checkout(page)
    (line,) = payload['items']
    assert line['combo_option_id'] == demo['combo_id'] and line['menu_item_id'] == demo['示範牛肉堡']
    assert page.locator('#receipt-total').inner_text() == '180'
    assert page.locator('#receipt-items-body').get_by_text(COMBO_NAME, exact=False).is_visible()

    order = order_by_uuid(app, payload['client_order_id'])
    assert order['total_price'] == 180
    ((name, qty, price, combo_id, customization),) = order['items']
    assert (name, qty, price, combo_id) == ('示範牛肉堡', 1, 180, demo['combo_id'])
    assert customization == f'[套餐: {COMBO_NAME}]、示範薯條(中辣)、示範紅茶(少冰/全糖)'


def test_addon_and_quantity_controls(app, page, demo):
    """加購品：有合格主餐才出現加購推薦，主餐移除後加購自動減量"""
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')

    add_from_menu(page, demo[XSS_ITEM])
    open_cart(page)
    addon_btn = page.locator(f'#addon-recommend-container [data-action="addon"][data-id="{demo["示範紅茶"]}"]')
    addon_btn.click()
    page.locator('#modifierModal.show').wait_for()
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('#modifierModal').wait_for(state='hidden')
    assert page.locator('#cart-items .badge', has_text='加購').count() == 1
    assert page.locator('#cart-final').inner_text() == str(99 + 25)
    # 名額用完：加購推薦中不再出現紅茶
    assert addon_btn.count() == 0
    # 加購品的「+」：名額已滿時提示，數量不變 (原本程式錯誤，按了沒反應；提示由 page 夾具按確定)
    with page.expect_event('dialog') as dialog_info:
        page.locator('#cart-items [data-action="qty"][data-index="1"][data-delta="1"]').click()
    assert '上限' in dialog_info.value.message
    expect(page.locator('#cart-count')).to_have_text('2')

    # 主餐數量歸零 → 加購品失去資格被自動移除
    page.locator('#cart-items [data-action="qty"][data-index="0"][data-delta="-1"]').click()
    expect(page.locator('#cart-count')).to_have_text('0')


def test_reward_item_in_cart_checkout_and_refund(app, page, demo):
    """紅利商城兌換的商品 (redeemed_item) 寫入購物車後可結帳；移除時退回點數"""
    from app.models.user import User

    def redeem_into_cart():
        token = page.locator('meta[name="csrf-token"]').get_attribute('content')
        resp = page.request.post('/api/redeem_reward', data={'item_id': demo['示範雞塊']}, headers={'X-CSRFToken': token})
        assert resp.ok, resp.text()
        line = resp.json()['redeemed_item']
        page.evaluate("""line => {
            const cart = JSON.parse(localStorage.getItem('kiosk_cart') || '[]');
            cart.push(line);
            localStorage.setItem('kiosk_cart', JSON.stringify(cart));
        }""", line)
        page.reload()
        page.wait_for_load_state('networkidle')

    def points():
        with app.app_context():
            return User.query.filter_by(phone=MEMBER_PHONE).first().points

    browser_member_login(page)
    page.goto('/')
    page.wait_for_load_state('networkidle')
    start = points()

    redeem_into_cart()
    assert points() == start - 30
    open_cart(page)
    assert page.locator('#cart-items').get_by_text('[點數兌換]').is_visible()
    payload = checkout(page)
    assert payload['items'][0]['is_reward'] is True
    order = order_by_uuid(app, payload['client_order_id'])
    assert [(n, q, p) for n, q, p, _c, _cu in order['items']] == [('示範雞塊', 1, 0)]

    # 再兌換一份後從購物車移除：點數退回
    page.locator('[data-action="finish"]').click()
    page.wait_for_load_state('networkidle')
    redeem_into_cart()
    before_refund = points()
    open_cart(page)
    page.locator('#cart-items [data-action="remove"]').click()
    expect(page.locator('#cart-count')).to_have_text('0')
    assert points() == before_refund + 30
    assert page.locator('#nav-user-points').inner_text() == str(before_refund + 30)


def test_logout_button_posts_and_idle_logout_uses_post(app, page, demo):
    """登出改為 POST (含 CSRF)：按鈕登出回到首頁；閒置登出以 fetch POST 後回首頁"""
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    with page.expect_request(lambda r: r.url.endswith('/logout') and r.method == 'POST'):
        page.get_by_role('button', name='登出').click()
    page.get_by_role('button', name=re.compile('訪客直接點餐')).wait_for()

    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    with page.expect_request(lambda r: r.url.endswith('/logout') and r.method == 'POST'):
        page.evaluate("Kiosk.idleLogout({ timeoutMs: 50 })")
    page.get_by_role('button', name=re.compile('訪客直接點餐')).wait_for()
