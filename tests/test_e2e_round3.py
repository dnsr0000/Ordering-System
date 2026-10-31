"""端對端 (第三輪)：點餐機 / POS 套餐任選、POS 混合付款、POS 作廢單一品項 (改單)"""
import re
import uuid
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import ComboOption, ComboOptionItem, MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import PaymentRecord
from tests.support import browser_staff_login, default_tenant_id, make_item, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture
def combo_menu(app):
    seed_demo(app)
    tag = uuid.uuid4().hex[:4]
    burger = make_item(app, f'E2E任選堡{tag}', price=120, stock=30, category='主餐')
    tea = make_item(app, f'E2E任選紅茶{tag}', price=30, stock=30, category='飲料')
    latte = make_item(app, f'E2E任選拿鐵{tag}', price=30, stock=30, category='飲料')
    with app.app_context():
        tid = default_tenant_id(app)
        combo = ComboOption(tenant_id=tid, main_item_id=burger, name=f'E2E任選餐{tag}', additional_price=40)
        db.session.add(combo)
        db.session.flush()
        db.session.add_all([ComboOptionItem(tenant_id=tid, combo_option_id=combo.id, menu_item_id=tea, choice_group='飲料'),
                            ComboOptionItem(tenant_id=tid, combo_option_id=combo.id, menu_item_id=latte, choice_group='飲料',
                                            position=1, extra_price=25)])
        db.session.commit()
        latte_choice = ComboOptionItem.query.filter_by(combo_option_id=combo.id, menu_item_id=latte).one().id
        return dict(burger=burger, tea=tea, latte=latte, combo=combo.id, latte_choice=latte_choice, tag=tag)


def _stock(app, item_id):
    with app.app_context():
        return db.session.get(MenuItem, item_id).stock


def test_kiosk_combo_choice(app, page, combo_menu):
    m = combo_menu
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    page.locator(f'#menuList .menu-row[data-id="{m["burger"]}"] [data-action="add"]').click()
    page.locator(f'#combo-cards-container [data-action="pick-combo"][data-combo-id="{m["combo"]}"]').click()
    page.locator('#modifierModal.show').wait_for()
    page.locator(f'label[for="choice-0-{m["latte_choice"]}"]').click()
    page.locator('[data-action="confirm-modifiers"]').click()
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    expect(page.locator('#cart-items')).to_contain_text(f'飲料:E2E任選拿鐵{m["tag"]}(+25)')
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    expect(page.locator('#preview-final')).to_have_text('185')
    with page.expect_request(lambda r: r.url.endswith('/submit_order') and r.method == 'POST') as req:
        page.locator('[data-action="checkout"]').click()
    page.locator('#receiptModal.show').wait_for()
    assert req.value.post_data_json['items'][0]['combo_choices'] == [m['latte_choice']]
    with app.app_context():
        order = Order.query.filter_by(client_order_id=req.value.post_data_json['client_order_id']).one()
        assert order.total_price == 185
    assert (_stock(app, m['latte']), _stock(app, m['tea'])) == (29, 30)


def _open_pos(page, username='e2e_pos'):
    browser_staff_login(page, username)
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')


def test_pos_combo_choice_and_split_payment(app, page, combo_menu):
    m = combo_menu
    _open_pos(page)
    page.locator('#posMenuGrid').get_by_text(f'E2E任選堡{m["tag"]}', exact=True).click()
    page.locator(f'#posComboCardsContainer [data-action="chooseCombo"][data-combo-id="{m["combo"]}"]').click()
    expect(page.locator('#posModifierModal')).to_be_visible()
    page.locator(f'label[for="pos-choice-0-{m["latte_choice"]}"]').click()
    page.locator('#posModifierConfirmBtn').click()
    expect(page.locator('#posFinalTotal')).to_have_text('185')
    expect(page.locator('#posCartList')).to_contain_text('飲料:E2E任選拿鐵')

    # 混合付款：現金 100 (實收 200) + 刷卡 85
    page.select_option('#posPaymentMethod', '混合付款')
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posTenderModal')).to_be_visible()
    rows = page.locator('#posTenderRows .pos-tender-row')
    rows.nth(0).locator('.tender-method').select_option('Cash')
    rows.nth(0).locator('.tender-amount').fill('100')
    rows.nth(0).locator('.tender-extra').fill('200')
    rows.nth(1).locator('.tender-method').select_option('Credit Card')
    rows.nth(1).locator('.tender-amount').fill('85')
    rows.nth(1).locator('.tender-extra').fill('AUTH77/4242')
    expect(page.locator('#posTenderRemain')).to_have_text('$0')
    page.locator('#posTenderSubmit').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    with app.app_context():
        o = db.session.get(Order, order_id)
        assert (o.payment_method, o.total_price, o.cash_change) == ('混合付款', 185, 100)
        recs = sorted((p.method, p.amount, p.reference) for p in PaymentRecord.query.filter_by(order_id=order_id))
        assert recs == [('Cash', 100, ''), ('Credit Card', 85, 'AUTH77')]


def test_pos_void_single_item(app, page, combo_menu):
    m = combo_menu
    soup = make_item(app, f'E2E作廢湯{m["tag"]}', price=50, stock=30, category='主餐')
    _open_pos(page, 'e2e_manager')
    page.locator('#posMenuGrid').get_by_text(f'E2E作廢湯{m["tag"]}', exact=True).click()
    page.locator('#posMenuGrid').get_by_text(f'E2E作廢湯{m["tag"]}', exact=True).click()
    page.locator('#posMenuGrid').get_by_text(f'E2E任選紅茶{m["tag"]}', exact=True).click()
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    page.locator('#rcpt-close-btn').click()

    page.locator('[data-action="openLiveOrders"]').click()
    card = page.locator(f'#posLiveOrdersGrid [data-order-id="{order_id}"]')
    card.locator('[data-action="openRefund"]').click()
    expect(page.locator('#posRefundModal')).to_be_visible()
    page.locator('label[for="posRefundItem"]').click()
    with app.app_context():
        soup_line = OrderItem.query.filter_by(order_id=order_id, menu_item_id=soup).one().id
    page.select_option('#posRefundItemSel', str(soup_line))
    page.fill('#posRefundItemQty', '1')
    page.fill('#posRefundReason', '客人少點一碗')
    page.locator('#posRefundForm button[type="submit"]').click()
    expect(page.locator('#posRefundModal')).to_be_hidden()
    expect(card).to_contain_text('作廢 1')
    with app.app_context():
        oi = db.session.get(OrderItem, soup_line)
        assert (oi.quantity, oi.voided_qty) == (1, 1)
        assert db.session.get(Order, order_id).refunded_amount == 50
    assert _stock(app, soup) == 29


# ===========================================================================
# 第 2 階段：桌位平面圖、桌邊線上結帳、寄杯
# ===========================================================================
def _store_setting(app, key, **values):
    from app.services import settings_service
    with app.app_context():
        settings_service.put(key, values, tenant_id=default_tenant_id(app))
        db.session.commit()


def test_admin_floor_plan_drag_and_pos_floor_view(app, page):
    from app.models.table import DiningTable
    seed_demo(app)
    tag = uuid.uuid4().hex[:3].upper()
    with app.app_context():
        tid = default_tenant_id(app)
        for n in (1, 2):
            db.session.add(DiningTable(tenant_id=tid, name=f'E{tag}{n}', area='E2E區'))
        db.session.commit()
        first = DiningTable.query.filter_by(name=f'E{tag}1').one().id
    browser_staff_login(page, 'e2e_manager')
    assert page.goto('/admin/tables').ok
    page.select_option('#floorArea', 'E2E區')
    table = page.locator(f'#floorPlanEditor .fp-table[data-table-id="{first}"]')
    page.locator('#floorPlanEditor [data-fp-canvas]').scroll_into_view_if_needed()
    table.hover()   # 先移到桌位 (可能捲動頁面)，再量平面圖的位置
    canvas = page.locator('#floorPlanEditor [data-fp-canvas]').bounding_box()
    page.mouse.down()
    page.mouse.move(canvas['x'] + canvas['width'] * 0.5, canvas['y'] + canvas['height'] * 0.4, steps=5)
    page.mouse.up()
    page.locator('[data-action="save-layout"]').click()
    expect(page.locator('#floorSaveMsg')).to_contain_text('已儲存')
    with app.app_context():
        t = db.session.get(DiningTable, first)
        assert 400 <= t.pos_x <= 600 and 300 <= t.pos_y <= 500

    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('[data-action="openTables"]').first.click()
    page.locator('#posTablesFloorBtn').click()
    expect(page.locator(f'#posFloorPlan .fp-table[data-table-id="{first}"]')).to_have_class(re.compile('fp-free'))


def test_kiosk_table_bill_and_online_pay_page(app, page):
    from app.models.table import DiningTable
    seed_demo(app)
    _store_setting(app, 'table_pay', enabled=True)
    item = make_item(app, f'E2E桌邊結帳{uuid.uuid4().hex[:4]}', price=110, stock=20, category='主餐')
    with app.app_context():
        t = DiningTable(tenant_id=default_tenant_id(app), name=f'EP{uuid.uuid4().hex[:3]}')
        db.session.add(t)
        db.session.commit()
        token = t.token
    try:
        page.goto(f'/t/{token}')
        page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
        page.wait_for_load_state('networkidle')
        page.locator(f'#menuList .menu-row[data-id="{item}"] [data-action="add"]').click()
        page.locator('[data-bs-target="#cart-sidebar"]').click()
        page.locator('#cart-sidebar.show').wait_for()
        page.locator('[data-action="preview"]').click()
        page.locator('#confirmModal.show').wait_for()
        page.locator('[data-action="checkout"]').click()
        page.locator('#receiptModal.show').wait_for()
        page.locator('[data-action="finish"]').click()
        page.locator('#tableBillBtn').click()
        expect(page.locator('#tableBillModal')).to_be_visible()
        expect(page.locator('#tableBillTotal')).to_have_text('$110')
        page.locator('#tablePayBtn').click()
        page.wait_for_url(re.compile('/table/pay/'))
        expect(page.locator('#tablePayAmount')).to_have_text('$110')
        assert page.locator('#ecpay-form input[name="ReturnURL"]').get_attribute('value').endswith('/payment/ecpay/table_notify')
    finally:
        _store_setting(app, 'table_pay', enabled=False)


def test_pos_sell_pass_and_redeem(app, page):
    from app.models.member import MemberPass, PassProduct
    from app.models.user import User
    seed_demo(app)
    tag = uuid.uuid4().hex[:4]
    coffee = make_item(app, f'E2E寄杯咖啡{tag}', price=80, stock=30, category='飲料')
    with app.app_context():
        tid = default_tenant_id(app)
        db.session.add(PassProduct(tenant_id=tid, name=f'E2E寄杯{tag}', menu_item_id=coffee, uses=5, price=350))
        phone = f'0967{uuid.uuid4().int % 1000000:06d}'
        db.session.add(User(tenant_id=tid, name=f'E2E寄杯會員{tag}', phone=phone, photo_path=''))
        db.session.commit()
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.fill('#posMemberQuery', phone)
    page.locator('[data-action="lookupMember"]').click()
    expect(page.locator('#posMemberName')).to_have_text(f'E2E寄杯會員{tag}')
    page.locator('[data-action="openPassSell"]').click()
    expect(page.locator('#posPassSellModal')).to_be_visible()
    page.select_option('#posPassProduct', label=f'E2E寄杯{tag} (5 次) $350')
    page.select_option('#posPassMethod', 'Cash')
    page.fill('#posPassCash', '500')
    page.locator('#posPassSellBtn').click()
    expect(page.locator('#posPassSellModal')).to_be_hidden()
    expect(page.locator('#posMemberPasses')).to_contain_text('剩 5')
    page.locator('#posMemberPasses [data-action="usePass"]').click()
    if page.locator('#posModifierModal.show').count():
        page.locator('#posModifierConfirmBtn').click()
    expect(page.locator('#posFinalTotal')).to_have_text('0')
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    with app.app_context():
        mp = MemberPass.query.filter_by(name=f'E2E寄杯{tag}').one()
        assert (mp.remaining, mp.total_uses) == (4, 5)


def test_admin_menu_engineering_and_branch_pages(app, page):
    seed_demo(app)
    tag = uuid.uuid4().hex[:4]
    star = make_item(app, f'E2E工程明星{tag}', price=150, stock=50, category=f'E2E工程{tag}', unit_cost=30)
    dog = make_item(app, f'E2E工程瘦狗{tag}', price=60, stock=50, category=f'E2E工程{tag}', unit_cost=55)
    from app.services.order_service import process_checkout
    with app.test_request_context('/'):
        from app.models.tenant import Tenant
        from app.tenant_context import _use_tenant
        tenant = Tenant.query.filter_by(slug='default').first()
        _use_tenant(tenant, remember=False)
        process_checkout(tenant_id=tenant.id, items=[{'menu_item_id': star, 'quantity': 8},
                                                     {'menu_item_id': dog, 'quantity': 1}])
    browser_staff_login(page, 'e2e_manager')
    assert page.goto('/admin/settings').ok
    page.get_by_role('link', name=re.compile('菜單工程分析')).first.click()
    page.wait_for_load_state('networkidle')
    page.select_option('select[name="category"]', f'E2E工程{tag}')
    page.get_by_role('button', name='查詢').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#meChart circle[data-me-point]')).to_have_count(2)
    expect(page.locator(f'#meTable tr[data-me-row="{star}"]')).to_have_attribute('data-cls', 'star')
    expect(page.locator(f'#meTable tr[data-me-row="{dog}"]')).to_have_attribute('data-cls', 'dog')
    assert page.goto('/admin/transfers').ok
    expect(page.locator('#transfersPage')).to_contain_text('單機模式請使用')
    assert page.goto('/admin/branch-menu').ok
    expect(page.locator('#branchMenuPage')).to_contain_text('單機模式')


def test_admin_marketing_form_and_member_opt_out(app, page):
    from tests.support import browser_member_login, make_member
    seed_demo(app)
    phone = '09' + uuid.uuid4().int.__str__()[:8]
    make_member(app, 'E2E行銷會員', phone, pin='2468')
    browser_staff_login(page, 'e2e_manager')
    assert page.goto('/admin/marketing').ok
    expect(page.locator('#segmentCards [data-segment="champion"]')).to_be_visible()
    expect(page.locator('#cmSendAt')).to_be_hidden()
    page.select_option('#cmKind', 'broadcast')
    expect(page.locator('#cmSendAt')).to_be_visible()
    expect(page.locator('#cmInactive')).to_be_hidden()
    page.fill('#cmName', 'E2E推播')
    page.fill('#cmMessage', '{name} 您好')
    page.locator('#campaignForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#campaignTable')).to_contain_text('E2E推播')
    page.context.clear_cookies()
    browser_member_login(page, phone=phone, pin='2468')
    assert page.goto('/rewards').ok
    expect(page.locator('#marketingOptIn')).to_be_checked()
    page.locator('#marketingOptIn').click()
    expect(page.locator('#marketingMsg')).to_contain_text('已關閉')
    with app.app_context():
        from app.models.user import User
        assert User.query.filter_by(phone=phone).one().marketing_opt_out is True


def test_device_remote_lock_overlay_and_api_key_page(app, page):
    from app.models.integration import Device
    seed_demo(app)
    with app.app_context():
        tid = default_tenant_id(app)
        d = Device(tenant_id=tid, name=f'E2E點餐機{uuid.uuid4().hex[:4]}', kind='kiosk', pair_code='246813',
                   pair_expires_at=datetime.now() + timedelta(minutes=10))
        db.session.add(d)
        db.session.commit()
        did = d.id
    page.goto('/device/pair')
    page.fill('#pairCode', '246813')
    page.locator('#devicePairForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('body')).to_have_attribute('data-device', '1')
    with app.app_context():
        dev = db.session.get(Device, did)
        dev.is_locked, dev.lock_message = True, 'E2E 維修中'
        db.session.commit()
    page.reload()
    expect(page.locator('#deviceLock')).to_contain_text('E2E 維修中', timeout=8000)
    with app.app_context():
        assert db.session.get(Device, did).last_seen_at is not None
        db.session.delete(db.session.get(Device, did))
        db.session.commit()
    page.context.clear_cookies()
    from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert page.goto('/admin/integrations').ok
    page.fill('#akName', 'E2E 金鑰')
    page.locator('#apiKeyForm input[value="orders:read"]').check()
    page.locator('#apiKeyForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#newApiKey code')).to_contain_text('kk_')
