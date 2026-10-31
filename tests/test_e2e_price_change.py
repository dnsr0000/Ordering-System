"""端對端：店家改價時，點餐機與 POS 畫面上的購物車價格已過時 — 送出時提示新金額並更新購物車，確認後以新金額結帳"""
import re
import uuid

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order
from tests.support import browser_staff_login, make_item, seed_demo

pytestmark = pytest.mark.e2e


def _wait_dialog(page, dialogs, text):
    for _ in range(100):
        if any(text in m for m in dialogs):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f'沒有出現「{text}」的提示：{dialogs}')


def _drop_expected_409(page):
    """送出時伺服器回 409 (金額已變動) 是這個測試預期的結果，不算頁面錯誤"""
    page.problems[:] = [p for p in page.problems if '409 (CONFLICT)' not in p]


def _set_price(app, item_id, price):
    with app.app_context():
        db.session.get(MenuItem, item_id).price = price
        db.session.commit()


def test_kiosk_price_changed_after_adding_to_cart(app, page):
    name = f'改價便當{uuid.uuid4().hex[:4]}'
    item = make_item(app, name, price=100, stock=50, category='主餐')
    dialogs = []
    page.on('dialog', lambda d: dialogs.append(d.message))   # page 夾具會按確定
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    page.locator(f'#menuList .menu-row[data-id="{item}"] [data-action="add"]').click()

    _set_price(app, item, 120)                              # 加入購物車之後店家改價
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    expect(page.locator('#preview-final')).to_have_text('100')
    page.locator('[data-action="checkout"]').click()
    _wait_dialog(page, dialogs, '目前應付 $120')
    expect(page.locator('#confirmModal')).to_be_hidden()
    expect(page.locator('#cart-sidebar.show')).to_be_visible()

    page.locator('[data-action="preview"]').click()        # 購物車已更新為新價格，確認後送出
    page.locator('#confirmModal.show').wait_for()
    expect(page.locator('#preview-final')).to_have_text('120')
    with page.expect_request(lambda r: r.url.endswith('/submit_order') and r.method == 'POST') as req:
        page.locator('[data-action="checkout"]').click()
    page.locator('#receiptModal.show').wait_for()
    oid = req.value.post_data_json['client_order_id']
    with app.app_context():
        assert Order.query.filter_by(client_order_id=oid).one().total_price == 120

    # 購物車留在瀏覽器時改價：重新整理頁面就以新價格重算 (不必等送出才發現)
    page.locator('[data-action="finish"]').click()
    page.goto('/?shop=default')
    page.wait_for_load_state('networkidle')
    guest = page.get_by_role('button', name=re.compile('訪客直接點餐'))
    if guest.count():
        guest.click()
        page.wait_for_load_state('networkidle')
    page.locator(f'#menuList .menu-row[data-id="{item}"] [data-action="add"]').click()
    _set_price(app, item, 90)
    page.reload()
    page.wait_for_load_state('networkidle')
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    expect(page.locator('#preview-final')).to_have_text('90')
    _drop_expected_409(page)


def test_pos_price_changed_after_adding_to_cart(app, page):
    seed_demo(app)   # 建立 e2e_pos 收銀帳號
    name = f'POS改價{uuid.uuid4().hex[:4]}'
    item = make_item(app, name, price=100, stock=50, category='主餐')
    dialogs = []
    page.on('dialog', lambda d: dialogs.append(d.message))   # page 夾具會按確定
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('#posMenuGrid').get_by_text(name, exact=True).click()
    expect(page.locator('#posFinalTotal')).to_have_text('100')

    _set_price(app, item, 130)
    page.locator('#posSubmitBtn').click()
    _wait_dialog(page, dialogs, '目前應付 $130')
    expect(page.locator('#posFinalTotal')).to_have_text('130')   # 購物車更新為新價格
    expect(page.locator('#posReceiptModal')).to_be_hidden()

    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert (order.total_price, order.cash_received) == (130, 130)
    _drop_expected_409(page)
