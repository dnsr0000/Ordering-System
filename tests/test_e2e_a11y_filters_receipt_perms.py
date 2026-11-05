"""端對端：顧客頁無障礙檢查 (axe-core：嚴重 / 重大違規即失敗)、點餐機飲食篩選、會員價劃線價、
點餐機禮物卡付款、收據視窗電子收據、廚房製作卡、評價回覆與公開好評、後台頁面權限 (櫃檯授權後看到的分頁)"""
import re

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order, OrderFeedback
from app.services import settings_service
from tests.support import browser_member_login, browser_staff_login, default_tenant_id, seed_demo

pytestmark = pytest.mark.e2e
try:
    from axe_playwright_python.sync_playwright import Axe
except ImportError:   # pragma: no cover
    Axe = None


@pytest.fixture(autouse=True)
def demo(app):
    ids = seed_demo(app)
    with app.app_context():
        tid = default_tenant_id(app)
        item = db.session.get(MenuItem, ids['示範薯條'])
        item.allergens, item.spicy_level, item.member_price, item.prep_note = 'gluten', 2, 50, '油溫 180 度炸 3 分鐘'
        db.session.get(MenuItem, ids['示範紅茶']).diet = 'vegan'
        settings_service.put('gift_card', dict(settings_service.get('gift_card', tid), enabled=True, kiosk_enabled=True), tid)
        settings_service.put('receipt', dict(settings_service.get('receipt', tid), email_enabled=False, line_enabled=False), tid)
        settings_service.put('feedback', dict(settings_service.get('feedback', tid), enabled=True), tid)
        db.session.commit()
    return ids


def _guest(page):
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')


def _axe_serious(page):
    results = Axe().run(page)
    return [(v['id'], v['impact'], v['nodes'][0]['target'] if v['nodes'] else '')
            for v in results.response['violations'] if v['impact'] in ('critical', 'serious')]


@pytest.mark.skipif(Axe is None, reason='未安裝 axe-playwright-python')
def test_customer_pages_have_no_serious_accessibility_violations(app, page, demo):
    problems = {}
    page.goto('/?shop=default')
    page.wait_for_load_state('networkidle')
    problems['landing'] = _axe_serious(page)
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    problems['kiosk'] = _axe_serious(page)
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    problems['cart'] = _axe_serious(page)
    for path in ('/register', '/waitlist', '/privacy', '/rewards', '/shop/default/group/new'):
        resp = page.goto(path)
        if resp and resp.ok:
            page.wait_for_load_state('networkidle')
            problems[path] = _axe_serious(page)
    page.problems[:] = [p for p in page.problems if '404' not in p and '302' not in p]
    problems = {k: v for k, v in problems.items() if v}
    assert problems == {}, f'顧客頁有嚴重 / 重大無障礙違規 (axe-core)：{problems}'


def test_kiosk_diet_filter_chips_hide_items_and_persist(app, page, demo):
    _guest(page)
    fries = page.locator(f'#menuList .menu-row[data-id="{demo["示範薯條"]}"]')
    tea = page.locator(f'#menuList .menu-row[data-id="{demo["示範紅茶"]}"]')
    expect(page.locator('#dietFilterBar')).to_be_visible()
    page.locator('#dietFilterBar [data-kind="mild"]').click()
    expect(fries).to_have_class(re.compile('d-none'))
    expect(tea).not_to_have_class(re.compile('d-none'))
    page.locator('#dietFilterBar [data-kind="veg"]').click()
    expect(page.locator(f'#menuList .menu-row[data-id="{demo["示範牛肉堡"]}"]')).to_have_class(re.compile('d-none'))
    expect(tea).not_to_have_class(re.compile('d-none'))
    # 重新整理後記住；清除後全部顯示
    page.reload()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#dietFilterBar [data-kind="veg"]')).to_have_attribute('aria-pressed', 'true')
    expect(fries).to_have_class(re.compile('d-none'))
    page.locator('[data-action="reset-filters"]').click()
    expect(fries).not_to_have_class(re.compile('d-none'))
    page.locator('#dietFilterBar [data-kind="allergen"][data-code="gluten"]').click()
    expect(fries).to_have_class(re.compile('d-none'))


def test_member_price_shown_only_for_member(app, page, demo):
    _guest(page)
    row = page.locator(f'#menuList .menu-row[data-id="{demo["示範薯條"]}"]')
    assert '會員價' not in row.inner_text() and '$60' in row.inner_text()
    browser_member_login(page)
    page.goto('/')
    page.wait_for_load_state('networkidle')
    row = page.locator(f'#menuList .menu-row[data-id="{demo["示範薯條"]}"]')
    expect(row).to_contain_text('會員價')
    assert '$50' in row.inner_text()
    row.locator('[data-action="add"]').click()
    modal = page.locator('#modifierModal.show')
    modal.wait_for()
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('#modifierModal').wait_for(state='hidden')
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    expect(page.locator('#cart-final')).to_have_text('50')


def test_kiosk_gift_card_balance_and_checkout(app, page, demo):
    from app.services import giftcard_service
    with app.app_context():
        with app.test_request_context('/'):
            from flask import g
            g.tenant_id = default_tenant_id(app)
            code = giftcard_service.sell(g.tenant_id, 500, 'Cash', 'e2e').code
            db.session.commit()
    _guest(page)
    page.locator(f'#menuList .menu-row[data-id="{demo["示範雞塊"]}"] [data-action="add"]').click()
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    page.select_option('#paymentMethod', '禮物卡')
    expect(page.locator('#giftCardBox')).to_be_visible()
    page.fill('#giftCardCode', code)
    page.locator('[data-action="gift-card-check"]').click()
    expect(page.locator('#giftCardBalance')).to_contain_text('500')
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    with page.expect_response(lambda r: r.url.endswith('/submit_order')) as resp:
        page.locator('[data-action="checkout"]').click()
    assert resp.value.status == 200
    page.locator('#receiptModal.show').wait_for()
    expect(page.locator('#receipt-payment')).to_contain_text('禮物卡')
    with app.app_context():
        o = Order.query.order_by(Order.id.desc()).first()
        assert o.payment_method == '禮物卡' and o.payment_reference == code


def test_kitchen_prep_card_modal(app, page, demo):
    from tests.support import login_staff, pos_checkout
    assert pos_checkout(login_staff(app, 'e2e_pos', 'staff'), demo['示範薯條']).status_code == 200
    browser_staff_login(page, 'e2e_kitchen')
    assert page.goto('/kitchen').ok
    card = page.locator('[data-action="prep-card"]').first
    card.wait_for()
    card.click()
    page.locator('#prepCardModal.show').wait_for()
    expect(page.locator('#prepCardBody')).to_contain_text('油溫 180 度炸 3 分鐘')
    expect(page.locator('#prepCardTitle')).to_contain_text('示範薯條')


def test_feedback_reply_and_public_reviews(app, page, demo):
    browser_staff_login(page, 'e2e_pos')
    token = page.locator('meta[name="csrf-token"]').get_attribute('content')
    body = page.request.post('/api/pos/checkout', data={'items': [{'menu_item_id': demo['示範雞塊'], 'quantity': 1}],
                                                        'payment_method': 'Cash', 'order_type': '內用', 'cash_received': 70},
                             headers={'X-CSRFToken': token}).json()
    with app.app_context():
        db.session.get(Order, body['order_id']).status = 'Completed'
        db.session.commit()
    # 顧客評分
    page.context.clear_cookies()
    page.goto(f'/track/{body["order_uuid"]}')
    page.locator('#feedbackBox').wait_for()
    page.locator('.feedback-star[data-value="5"]').click()
    page.fill('#feedbackComment', '雞塊超讚')
    page.locator('[data-action="send-feedback"]').click()
    expect(page.locator('#feedbackMsg')).to_contain_text('謝謝')
    # 店長回覆並公開
    browser_staff_login(page, 'e2e_manager')
    assert page.goto('/admin/feedback').ok
    with app.app_context():
        fb_id = OrderFeedback.query.filter_by(order_id=body['order_id']).first().id
    form = page.locator(f'#fb-{fb_id} .feedback-reply-form')
    form.locator('input[name="reply"]').fill('謝謝！歡迎再來')
    form.locator('input[name="is_public"]').check()
    form.locator('button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator(f'#fb-{fb_id}')).to_contain_text('公開')
    # 顧客追蹤頁看到回覆；未登入頁看到好評
    page.context.clear_cookies()
    page.goto(f'/track/{body["order_uuid"]}')
    expect(page.locator('#feedbackReply')).to_contain_text('謝謝！歡迎再來')
    page.goto('/?shop=default')
    expect(page.locator('#publicReviews')).to_contain_text('雞塊超讚')
    assert '示範會員' not in page.locator('#publicReviews').inner_text()


def test_staff_with_menu_permission_sees_only_granted_tabs(app, page, demo):
    from app.models.tenant import StaffAccount
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='e2e_pos').first()
        acc.permissions = 'menu'
        db.session.commit()
    try:
        browser_staff_login(page, 'e2e_pos')
        assert page.goto('/admin').ok
        expect(page.locator('#home-pane')).to_be_visible()
        assert page.locator('#orders-pane').count() == 0
        expect(page.locator('#nav-menu-pro')).to_be_visible()
        assert page.goto('/admin/menu').ok
        expect(page.locator('#menu-pane')).to_be_visible()
        resp = page.goto('/admin/members')
        assert resp.status == 403
        page.problems[:] = [p for p in page.problems if '403 (FORBIDDEN)' not in p]
    finally:
        with app.app_context():
            StaffAccount.query.filter_by(username='e2e_pos').first().permissions = ''
            db.session.commit()
