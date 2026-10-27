"""端對端：第二批專業功能 — 餐點規格 (自助點餐 / POS)、客顯螢幕同步、會員動態條碼 (紅利商城出示、POS 掃描帶出)"""
import re

import pytest
from playwright.sync_api import expect

from tests.support import browser_member_login, browser_staff_login, make_item, make_member, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture
def sized_item(app):
    seed_demo(app)
    return make_item(app, 'E2E 規格奶茶', price=50, stock=50, category='飲料', size_options='中杯:0, 大杯:+15')


def test_kiosk_size_choice_changes_price(app, page, sized_item):
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    page.locator(f'#menuList .menu-row[data-id="{sized_item}"] [data-action="add"]').click()
    page.locator('#modifierModal.show').wait_for()
    page.locator('#modifierModalBody label', has_text='大杯').click()
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('#modifierModal').wait_for(state='hidden')
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    expect(page.locator('#cart-items')).to_contain_text('大杯')
    expect(page.locator('#cart-final')).to_have_text('65')
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    with page.expect_request(lambda r: r.url.endswith('/submit_order') and r.method == 'POST') as req:
        page.locator('[data-action="checkout"]').click()
    page.locator('#receiptModal.show').wait_for()
    assert req.value.post_data_json['items'][0]['size'] == '大杯'
    expect(page.locator('#receipt-total')).to_have_text('65')


def test_pos_customer_display_follows_cart_and_payment(app, page, sized_item):
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    with page.context.expect_page() as popup:
        page.locator('#posDisplayBtn').click()
    display = popup.value
    display.on('pageerror', lambda err: page.problems.append(f'客顯 JS 錯誤: {err}'))
    display.wait_for_load_state('networkidle')
    expect(display.locator('#customerDisplay')).to_have_attribute('data-state', 'idle')
    expect(display.locator('#cdWelcome')).to_have_text('歡迎光臨')

    page.locator(f'#pos-card-{sized_item}').click()
    expect(page.locator('#posModifierModal')).to_be_visible()
    page.locator('#posModifierModal label', has_text='大杯').click()
    page.locator('#posModifierConfirmBtn').click()
    expect(page.locator('#posFinalTotal')).to_have_text('65')
    expect(display.locator('#customerDisplay')).to_have_attribute('data-state', 'cart')
    expect(display.locator('#cdLines')).to_contain_text('E2E 規格奶茶')
    expect(display.locator('#cdLines')).to_contain_text('大杯')
    expect(display.locator('#cdTotal')).to_have_text('$65')

    page.select_option('#posPaymentMethod', 'Cash')
    page.fill('#posCashReceived', '100')
    expect(display.locator('#cdChange')).to_have_text('$35')
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    expect(display.locator('#customerDisplay')).to_have_attribute('data-state', 'paid')
    expect(display.locator('#cdPaidChange')).to_have_text('找您 $35')
    page.locator('#rcpt-close-btn').click()


def test_member_qr_shown_on_phone_and_scanned_at_pos(app, page):
    seed_demo(app)
    make_member(app, 'E2E 條碼會員', '0968800123', points=77, pin='1357')
    browser_member_login(page, phone='0968800123', pin='1357')
    assert page.goto('/rewards').ok
    page.wait_for_load_state('networkidle')
    page.locator('#memberQrBtn').click()
    img = page.locator('#memberQrImg')
    expect(img).to_have_attribute('src', re.compile('^data:image/svg'))
    token = img.get_attribute('data-token')
    assert token.startswith('KM1.')

    pos = page.context.browser.new_context(base_url=page.url.split('/rewards')[0], viewport={'width': 1366, 'height': 900})
    try:
        staff = pos.new_page()
        staff.on('dialog', lambda d: d.accept())
        browser_staff_login(staff, 'e2e_pos')
        assert staff.goto('/pos').ok
        staff.wait_for_load_state('networkidle')
        staff.fill('#posMemberQuery', token)          # 掃碼槍輸入後按 Enter
        staff.locator('#posMemberQuery').press('Enter')
        expect(staff.locator('#posMemberPoints')).to_have_text('77')
        expect(staff.locator('body')).to_contain_text('E2E 條碼會員')
    finally:
        pos.close()


# ---------------------------------------------------------------------------
# 第三批：待機輪播、大字模式、熱量合計、桌邊呼叫服務、餐後評分
# ---------------------------------------------------------------------------
def test_kiosk_attract_and_large_text_and_calories(app, page, store_settings):
    import os
    from tests.support import jpeg_bytes
    seed_demo(app)
    with open(os.path.join(app.config['UPLOAD_FOLDER_MENU'], 'e2e_attract.jpg'), 'wb') as fh:
        fh.write(jpeg_bytes().getvalue())
    make_item(app, 'E2E 輪播特餐', price=88, image_path='e2e_attract.jpg', is_recommended=True, calories=450)
    store_settings('menu', attract_seconds=2, attract_message='摸我開始點餐')
    page.goto('/?shop=default')
    overlay = page.locator('#attractOverlay')
    expect(overlay).to_be_visible(timeout=6000)
    expect(page.locator('#attractName')).not_to_be_empty()
    overlay.click()
    expect(overlay).to_be_hidden()

    page.locator('#a11yToggleLanding').click()
    expect(page.locator('html')).to_have_class(re.compile('a11y-large'))
    page.reload()
    expect(page.locator('html')).to_have_class(re.compile('a11y-large'))
    expect(page.locator('#a11yToggleLanding')).to_have_attribute('aria-pressed', 'true')

    store_settings('menu', attract_seconds=0)
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    row = page.locator('#menuList .menu-row', has_text='E2E 輪播特餐')
    expect(row).to_contain_text('450 kcal')
    row.locator('[data-action="add"]').first.click()
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    expect(page.locator('#cart-calories')).to_have_text('450')


def test_table_call_reaches_pos_and_track_feedback(app, page, admin_client, store_settings):
    from app.extensions import db
    from app.models.order import Order
    from app.models.table import DiningTable
    from tests.support import line, login_staff
    seed_demo(app)
    admin_client.post('/admin/tables', data={'action': 'create', 'names': 'E2E9'})
    with app.app_context():
        token = DiningTable.query.filter_by(name='E2E9').first().token
    page.goto(f'/t/{token}')
    page.wait_for_load_state('networkidle')
    page.locator('#serviceCallBox [data-kind="water"]').click()
    page.wait_for_timeout(500)

    pos_ctx = page.context.browser.new_context(base_url=page.url.split('/?')[0].split('/t/')[0], viewport={'width': 1366, 'height': 900})
    try:
        pos = pos_ctx.new_page()
        pos.on('dialog', lambda d: d.accept())
        browser_staff_login(pos, 'e2e_pos')
        assert pos.goto('/pos').ok
        pos.wait_for_load_state('networkidle')
        expect(pos.locator('#posCallsBtn')).to_have_class(re.compile('btn-danger'))
        pos.locator('#posCallsBtn').click()
        row = pos.locator('#posCallsList .pos-call-row', has_text='E2E9')
        expect(row).to_contain_text('加水')
        row.locator('[data-action="serviceCallDone"]').click()
        expect(pos.locator('#posCallsList .pos-call-row', has_text='E2E9')).to_have_count(0)
    finally:
        pos_ctx.close()

    store_settings('feedback', enabled=True, google_review_url='https://g.page/r/e2e', review_min_rating=4)
    staff = login_staff(app, 'e2e_fb_pos', 'staff')
    item = make_item(app, 'E2E 評分餐', price=70)
    res = staff.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用',
                                                'cash_received': 70}).get_json()
    with app.app_context():
        db.session.get(Order, res['order_id']).status = 'PickedUp'
        db.session.commit()
    page.goto(f"/track/{res['order_uuid']}")
    expect(page.locator('#feedbackBox')).to_be_visible()
    page.locator('.feedback-star[data-value="5"]').click()
    page.fill('#feedbackComment', '很好吃')
    page.locator('#feedbackSendBtn').click()
    expect(page.locator('#feedbackMsg')).to_contain_text('謝謝')
    expect(page.locator('#feedbackReview')).to_have_attribute('href', 'https://g.page/r/e2e')


# ---------------------------------------------------------------------------
# 第四批：禮物卡 (POS 售出與扣款)、集點卡與會員自助刪除帳號
# ---------------------------------------------------------------------------
def test_pos_gift_card_sell_and_pay(app, page, store_settings):
    seed_demo(app)
    store_settings('gift_card', enabled=True, preset_amounts='300, 500')
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('#posGiftBtn').click()
    expect(page.locator('#posGiftModal')).to_be_visible()
    page.locator('[data-action="giftPreset"][data-value="300"]').click()
    page.uncheck('#posGiftPrint')
    page.locator('#posGiftSellBtn').click()
    expect(page.locator('#posGiftNewCode')).to_be_visible()
    code = page.locator('#posGiftNewCode').inner_text().replace(' ', '')
    assert len(code) == 16
    page.fill('#posGiftLookupCode', code)
    page.locator('#posGiftLookupForm button[type="submit"]').click()
    expect(page.locator('#posGiftLookupMsg')).to_contain_text('餘額 $300')
    page.keyboard.press('Escape')
    expect(page.locator('#posGiftModal')).to_be_hidden()

    page.locator('#posMenuGrid').get_by_text('示範雞塊', exact=True).click()
    page.select_option('#posPaymentMethod', '禮物卡')
    page.evaluate('(c) => { window.prompt = () => c; }', code)   # 模擬刷卡輸入卡號
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    from app.models.member import GiftCard
    from app.services import giftcard_service
    with app.app_context():
        assert giftcard_service.balance(GiftCard.query.filter_by(code=code).one()) == 230


def test_rewards_stamp_card_and_self_delete(app, page, store_settings):
    from app.extensions import db
    from app.models.user import User
    seed_demo(app)
    store_settings('stamps', enabled=True, card_size=5, card_title='E2E 集點卡')
    make_member(app, 'E2E 刪帳號', '0968800456', pin='1357')
    browser_member_login(page, phone='0968800456', pin='1357')
    assert page.goto('/rewards').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#stampCard')).to_contain_text('E2E 集點卡')
    expect(page.locator('#stampCard .stamp-dot')).to_have_count(5)
    with page.expect_download() as dl:
        page.locator('#myDataDownload').click()
    assert dl.value.suggested_filename == 'my_member_data.json'
    page.locator('[data-bs-target="#deleteAccountBox"]').click()
    page.fill('#deleteAccountBox input[name="pin"]', '1357')
    page.fill('#deleteAccountBox input[name="confirm"]', '刪除')
    page.locator('#deleteAccountBtn').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('body')).to_contain_text('會員帳號與個人資料已刪除')
    with app.app_context():
        assert User.query.filter_by(phone='0968800456').first() is None


# ---------------------------------------------------------------------------
# 第五批：建議採購 → 送出 → 收貨；即時看板自動更新
# ---------------------------------------------------------------------------
def test_purchase_order_flow_and_live_dashboard(app, page, admin_client, store_settings):
    from app.extensions import db
    from app.models.inventory import Ingredient
    from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
    seed_demo(app)
    store_settings('inventory', enabled=True)
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': 'E2E 麵粉', 'unit': 'g', 'stock': '100',
                                                'low_threshold': '500', 'unit_cost': '0.05'})
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert page.goto('/admin/purchasing').ok
    page.fill('#addSupplierForm input[name="name"]', 'E2E 麵粉行')
    page.locator('#addSupplierForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('.supplier-row input[name="name"][value="E2E 麵粉行"]')).to_have_count(1)
    expect(page.locator('#suggestTable')).to_contain_text('E2E 麵粉')
    page.locator('#suggestBtn').click()
    page.wait_for_load_state('networkidle')
    page.locator('#poTable a').first.click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#poStatus')).to_have_text('草稿')
    page.locator('#poSendBtn').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#poStatus')).to_have_text('已送出')
    page.locator('#poReceiveBtn').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#poStatus')).to_have_text('已收貨')
    with app.app_context():
        assert Ingredient.query.filter_by(name='E2E 麵粉').one().stock > 100

    assert page.goto('/admin/live').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#liveStores tbody tr').first).to_be_visible()
    expect(page.locator('#liveHourly .bar-row').first).to_be_visible()


# ---------------------------------------------------------------------------
# 第六批：POS 電話訂餐外送到府 → 外送派車
# ---------------------------------------------------------------------------
def test_pos_home_delivery_and_dispatch(app, page, store_settings):
    seed_demo(app)
    store_settings('own_delivery', enabled=True, zones='近:30:100', drivers='阿明', channels=['pos'])
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('#posMenuGrid').get_by_text('示範雞塊', exact=True).click()
    page.locator('#posMenuGrid').get_by_text('示範雞塊', exact=True).click()
    page.locator('label[for="posTypeHome"]').click()
    expect(page.locator('#posHomeBox')).to_be_visible()
    page.fill('#posHomeAddress', '台南市東區大學路 1 號')
    page.fill('#posHomePhone', '0912345678')
    expect(page.locator('#posDeliveryFeeRow')).to_be_visible()
    expect(page.locator('#posFinalTotal')).to_have_text('170')
    page.select_option('#posPaymentMethod', 'Cash')
    page.fill('#posCashReceived', '200')
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    page.locator('#rcpt-close-btn').click()

    assert page.goto('/admin/dispatch').ok
    card = page.locator(f'.dispatch-card[data-order-id="{order_id}"]')
    expect(card).to_contain_text('大學路')
    card.locator('select[name="driver"]').select_option('阿明')
    card.locator('.dispatch-assign').click()
    page.wait_for_load_state('networkidle')
    card = page.locator(f'.dispatch-card[data-order-id="{order_id}"]')
    card.locator('.dispatch-depart').click()
    page.wait_for_load_state('networkidle')
    card = page.locator(f'.dispatch-card[data-order-id="{order_id}"]')
    card.locator('.dispatch-complete').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator(f'.dispatch-card[data-order-id="{order_id}"] .dispatch-status')).to_have_text('已送達')
