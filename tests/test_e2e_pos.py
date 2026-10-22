"""端對端：櫃檯 POS 實際操作 (點餐、客製化、套餐、加購、會員查詢、優惠碼、現金找零、收據與 XSS 顯示)"""
import random
import uuid

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import ComboOption, MenuItem, ModifierOption
from app.models.order import Order
from app.models.user import User
from tests.support import browser_staff_login, default_tenant_id, make_member, seed_demo

pytestmark = pytest.mark.e2e

XSS_ITEM = "O'Brien 的「特製」<script>alert(1)</script>"
XSS_MEMBER = "示範會員 </script><b>王'小明'"
XSS_COMBO = "A套餐(薯條+紅茶) <i>'含飲料'</i>"


@pytest.fixture(autouse=True)
def demo_data(app):
    return seed_demo(app)


def open_pos(page):
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#posCartCount')).to_have_text('0')


def click_item(page, name):
    page.locator('#posMenuGrid').get_by_text(name, exact=True).click()


def receipt_order(app, page):
    """等收據出現後，依收據上的系統單號從資料庫取出訂單 (回傳 (order, items) 的快照)"""
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order is not None
        items = [(i.item_name, i.price, i.quantity, i.customization, i.combo_option_id, i.is_reward) for i in order.items]
        snapshot = {c.name: getattr(order, c.name) for c in Order.__table__.columns}
    return snapshot, items


def close_receipt(page):
    page.locator('#rcpt-close-btn').click()
    expect(page.locator('#posReceiptModal')).to_be_hidden()


def test_pos_cash_checkout_with_customization(app, page):
    """一般商品 + 客製化飲料、現金 500 結帳，找零與資料庫訂單 (來源 POS) 正確，結帳後收銀台重置"""
    tag = uuid.uuid4().hex[:6]
    open_pos(page)

    click_item(page, '示範雞塊')                      # 無客製化：直接入單 $70
    expect(page.locator('#posCartCount')).to_have_text('1')

    click_item(page, '示範紅茶')                      # 冰塊/甜度：跳出客製化視窗 $40
    modal = page.locator('#posModifierModal')
    expect(modal).to_be_visible()
    modal.locator('select.mod-ice').select_option('少冰')
    modal.locator('select.mod-sugar').select_option('半糖')
    page.locator('#posModifierConfirmBtn').click()
    expect(modal).to_be_hidden()

    cart = page.locator('#posCartList')
    expect(cart).to_contain_text('少冰/半糖')
    expect(page.locator('#posCartCount')).to_have_text('2')
    expect(page.locator('#posSubtotal')).to_have_text('110')
    expect(page.locator('#posFinalTotal')).to_have_text('110')

    # 數量 +1 再 -1，金額跟著變
    cart.locator('[data-action="cartQty"][data-delta="1"]').first.click()
    expect(page.locator('#posFinalTotal')).to_have_text('180')
    cart.locator('[data-action="cartQty"][data-delta="-1"]').first.click()
    expect(page.locator('#posFinalTotal')).to_have_text('110')

    # 實收不足時顯示差額；快速鍵 $500 後找零 390
    page.fill('#posCashReceived', '100')
    expect(page.locator('#posChangeDisplay')).to_have_text('差 $10')
    page.locator('[data-action="quickCash"][data-mode="500"]').click()
    expect(page.locator('#posChangeDisplay')).to_have_text('找零 $390')

    page.fill('#posTableInput', f'E2E-{tag}')
    page.fill('#posOrderNote', '少油 <b>不要粗體</b>')
    page.locator('#posSubmitBtn').click()

    order, items = receipt_order(app, page)
    expect(page.locator('#rcpt-total')).to_have_text('110')
    expect(page.locator('#rcpt-received')).to_have_text('500')
    expect(page.locator('#rcpt-change')).to_have_text('390')
    expect(page.locator('#rcpt-note-box')).to_have_text('備註：少油 <b>不要粗體</b>')
    expect(page.locator('#rcpt-items-tbody')).to_contain_text('少冰/半糖')

    assert order['order_source'] == 'POS'
    assert order['cashier_username'] == 'e2e_pos'
    assert (order['total_price'], order['cash_received'], order['cash_change']) == (110, 500, 390)
    assert order['payment_method'] == 'Cash' and order['table_number'] == f'E2E-{tag}'
    assert order['uuid']  # 有帶 client_order_id
    by_name = {name: (price, qty, custom) for name, price, qty, custom, _combo, _reward in items}
    assert by_name['示範雞塊'] == (70, 1, '')
    assert by_name['示範紅茶'] == (40, 1, '少冰/半糖')

    # 收銀台已重置，可接續下一筆
    close_receipt(page)
    expect(page.locator('#posCartCount')).to_have_text('0')
    expect(page.locator('#posFinalTotal')).to_have_text('0')
    expect(page.locator('#posTableInput')).to_have_value('')
    expect(page.locator('#posCashReceived')).to_have_value('')

    # 當日訂單視窗可看到剛剛的訂單
    page.locator('[data-action="openLiveOrders"]').click()
    expect(page.locator(f'#posLiveOrdersGrid [data-order-id="{order["id"]}"]')).to_contain_text('示範紅茶')


def test_pos_member_lookup_and_promo_code(app, page):
    """以電話查詢會員並綁定、套用優惠碼 E2E20 結帳；同名多筆時以 posMemberSelectModal 選擇"""
    with app.app_context():
        member = User.query.filter_by(phone='0911222333').first()
        member_id, points_before = member.id, member.points
    tag = uuid.uuid4().hex[:6]
    twins = [make_member(app, f'E2E同名{tag}', f'09{random.randint(10**7, 10**8 - 1)}') for _ in range(2)]

    open_pos(page)

    # 同名多筆：跳出候選清單，選擇第二位
    page.fill('#posMemberQuery', f'E2E同名{tag}')
    page.locator('#posMemberLookupBtn').click()
    select_modal = page.locator('#posMemberSelectModal')
    expect(select_modal).to_be_visible()
    expect(select_modal.locator('.pos-member-candidate')).to_have_count(2)
    select_modal.locator(f'[data-action="selectMember"][data-id="{twins[1]}"]').click()
    expect(select_modal).to_be_hidden()
    expect(page.locator('#posMemberName')).to_have_text(f'E2E同名{tag}')
    page.locator('[data-action="clearMember"]').click()
    expect(page.locator('#posMemberStatusBadge')).to_have_text('未綁定 (一般訪客)')

    # 完整電話：直接綁定 (名稱含 HTML 特殊字元，必須顯示為純文字)
    page.fill('#posMemberQuery', '0911222333')
    page.keyboard.press('Enter')
    expect(page.locator('#posMemberStatusBadge')).to_have_text('已綁定會員')
    expect(page.locator('#posMemberName')).to_have_text(XSS_MEMBER)
    expect(page.locator('#posMemberPhone')).to_have_text('(0911222333)')
    expect(page.locator('#posMemberPoints')).to_have_text(str(points_before))

    # 牛肉堡有套餐：選單點 (特價 $120)
    click_item(page, '示範牛肉堡')
    combo_modal = page.locator('#posComboModal')
    expect(combo_modal).to_be_visible()
    combo_modal.locator('[data-action="chooseCombo"][data-combo-id="0"]').click()
    expect(combo_modal).to_be_hidden()
    expect(page.locator('#posModifierModal')).to_be_visible()   # 牛肉堡可加料
    page.locator('#posModifierConfirmBtn').click()
    expect(page.locator('#posSubtotal')).to_have_text('120')

    # 套用公開優惠碼
    page.fill('#posPromoInput', 'E2E20')
    page.locator('#posPromoApplyBtn').click()
    expect(page.locator('#posPromoMsg')).to_contain_text('現折 $20')
    expect(page.locator('#posPromoDiscount')).to_have_text('20')
    expect(page.locator('#posFinalTotal')).to_have_text('100')

    page.locator('[data-action="quickCash"][data-mode="exact"]').click()
    expect(page.locator('#posChangeDisplay')).to_have_text('找零 $0')
    page.keyboard.press('F2')                                     # 快捷鍵結帳

    order, items = receipt_order(app, page)
    expect(page.locator('#rcpt-discount')).to_have_text('20')
    expect(page.locator('#rcpt-total')).to_have_text('100')
    expect(page.locator('#rcpt-points-box')).to_be_visible()
    assert order['user_id'] == member_id
    assert order['coupon_code'] == 'E2E20'
    assert (order['total_price'], order['discount_amount']) == (100, 20)
    assert order['order_source'] == 'POS'
    assert [(n, p) for n, p, *_ in items] == [('示範牛肉堡', 120)]
    with app.app_context():
        assert db.session.get(User, member_id).points == points_before + order['points_earned']

    # 結帳後自動解除會員綁定與優惠碼
    close_receipt(page)
    expect(page.locator('#posMemberStatusBadge')).to_have_text('未綁定 (一般訪客)')
    expect(page.locator('#posPromoInput')).to_have_value('')


def test_pos_combo_and_addon_order(app, page):
    """升級套餐 (配餐客製化 + 主餐加料) 再加購飲料，以信用卡結帳；配餐一併扣庫存"""
    tag = uuid.uuid4().hex[:6]
    addon_name = f'加起司{tag}'
    with app.app_context():
        tid = default_tenant_id(app)
        db.session.add(ModifierOption(tenant_id=tid, category='addons', name=addon_name, price=15, is_active=True))
        db.session.commit()
        burger = MenuItem.query.filter_by(name='示範牛肉堡').first()
        combo_id = ComboOption.query.filter_by(main_item_id=burger.id).first().id
        fries_stock = MenuItem.query.filter_by(name='示範薯條').first().stock

    open_pos(page)
    click_item(page, '示範牛肉堡')
    combo_modal = page.locator('#posComboModal')
    expect(combo_modal).to_be_visible()
    expect(combo_modal.get_by_text(XSS_COMBO, exact=True)).to_be_visible()     # 套餐名稱為純文字
    combo_modal.locator(f'[data-action="chooseCombo"][data-combo-id="{combo_id}"]').click()

    modal = page.locator('#posModifierModal')
    expect(modal).to_be_visible()
    modal.locator('[data-group="main"]').get_by_label(f'{addon_name} (+$15)').check()
    modal.locator('[data-group="side0"] select.mod-spicy').select_option('中辣')
    modal.locator('[data-group="side1"] select.mod-ice').select_option('去冰')
    modal.locator('[data-group="side1"] select.mod-sugar').select_option('無糖')
    page.locator('#posModifierConfirmBtn').click()
    expect(modal).to_be_hidden()
    expect(page.locator('#posFinalTotal')).to_have_text('195')       # 120 + 套餐 60 + 加料 15
    expect(page.locator('#posCartList')).to_contain_text(XSS_COMBO)

    # 主餐可加購飲料 (加購價 $25)
    addon_btn = page.locator('#posAddonRecommendBox [data-action="addon"]').filter(has_text='示範紅茶')
    expect(addon_btn).to_contain_text('(餘1)')
    addon_btn.click()
    expect(modal).to_be_visible()
    page.locator('#posModifierConfirmBtn').click()
    expect(page.locator('#posCartList')).to_contain_text('加購')
    expect(page.locator('#posFinalTotal')).to_have_text('220')
    expect(page.locator('#posAddonRecommendBox [data-action="addon"]')).to_have_count(0)   # 名額用完

    page.select_option('#posPaymentMethod', 'Credit Card')
    expect(page.locator('#posQuickCashRow')).to_be_hidden()
    page.locator('#posSubmitBtn').click()

    order, items = receipt_order(app, page)
    expect(page.locator('#rcpt-total')).to_have_text('220')
    expect(page.locator('#rcpt-items-tbody')).to_contain_text(XSS_COMBO)
    assert order['payment_method'] == 'Credit Card' and order['cash_change'] == 0
    assert order['order_source'] == 'POS' and order['total_price'] == 220
    combo_line = next(i for i in items if i[4] == combo_id)
    assert combo_line[1] == 195
    for part in (f'[套餐: {XSS_COMBO}]', '示範薯條(中辣)', '示範紅茶(去冰/無糖)', f'{addon_name}(+15)'):
        assert part in combo_line[3], combo_line[3]
    addon_line = next(i for i in items if i[4] is None)
    assert addon_line[:3] == ('示範紅茶', 25, 1)
    with app.app_context():
        assert MenuItem.query.filter_by(name='示範薯條').first().stock == fries_stock - 1


def test_pos_xss_names_render_as_text(app, page):
    """含 <script>、引號的餐點名稱在菜單、購物車、收據、當日訂單都顯示為純文字，且不會執行"""
    dialogs = []
    page.on('dialog', lambda d: dialogs.append(d.message))
    api_calls = []
    page.on('request', lambda req: api_calls.append(req.url)
            if '/api/' in req.url and '/pos' in (req.headers.get('referer') or '') else None)   # 只記錄 POS 頁發出的請求
    open_pos(page)

    grid_name = page.locator('#posMenuGrid').get_by_text(XSS_ITEM, exact=True)
    expect(grid_name).to_be_visible()

    # 搜尋過濾
    page.fill('#posSearchInput', "O'Brien")
    expect(page.locator('.pos-menu-col:not(.d-none)')).to_have_count(1)
    grid_name.click()
    page.fill('#posSearchInput', '')
    page.keyboard.press('Escape')

    cart = page.locator('#posCartList')
    expect(cart.get_by_text(XSS_ITEM, exact=True)).to_be_visible()
    assert cart.locator('script').count() == 0
    expect(page.locator('#posFinalTotal')).to_have_text('99')

    page.locator('#posSubmitBtn').click()
    order, items = receipt_order(app, page)
    expect(page.locator('#rcpt-items-tbody').get_by_text(XSS_ITEM, exact=True)).to_be_visible()
    assert items[0][0] == XSS_ITEM and order['order_source'] == 'POS'
    close_receipt(page)

    page.locator('[data-action="openLiveOrders"]').click()
    live = page.locator(f'#posLiveOrdersGrid [data-order-id="{order["id"]}"]')
    expect(live).to_contain_text(XSS_ITEM)
    assert page.locator('#posLiveOrdersGrid script').count() == 0
    assert '1' not in dialogs, '名稱中的 <script> 被執行了'
    # 當日訂單與即時庫存走 POS 專用的輕量 API，不再呼叫會計算整份營運分析的後台 API
    assert any('/api/pos/today_orders' in u for u in api_calls)
    assert not any('/api/admin_live_orders' in u for u in api_calls)
