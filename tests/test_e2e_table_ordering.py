"""端對端：桌位 QR code — 後台建立桌位與列印桌卡、顧客手機 (小螢幕) 掃碼點餐、POS 桌位結帳"""
import re

import pytest
from playwright.sync_api import expect

from tests.support import browser_staff_login, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture
def demo(app):
    from app.extensions import db
    from app.models.menu import MenuItem
    ids = seed_demo(app)
    with app.app_context():
        item = db.session.get(MenuItem, ids['示範雞塊'])
        item.stock, item.total_stock, item.is_sold_out = 50, max(item.total_stock or 0, 50), False
        db.session.commit()
    return ids


def _table_token(app, name):
    from app.models.table import DiningTable
    with app.app_context():
        return DiningTable.query.filter_by(name=name).first().token


def test_manage_tables_scan_order_and_settle(app, page, demo):
    # 1. 店長建立桌位並開啟列印頁
    browser_staff_login(page, 'e2e_manager')
    page.goto('/admin/tables')
    page.locator('#table-names').fill('E1-E2')
    page.locator('#createTablesForm button[type=submit]').click()
    expect(page.locator('#tablesTable input[name="name"][value="E1"]')).to_have_count(1)
    with page.expect_popup() as popup_info:
        page.locator('#print-all-link').click()
    printout = popup_info.value
    printout.wait_for_load_state()
    expect(printout.locator('.qr-card[data-table="E1"] img.qr-image')).to_be_visible()
    printout.close()
    page.goto('/admin')
    page.locator('form[action$="/admin/logout"] button').click()

    # 2. 顧客用手機掃 E1 的 QR code，以訪客點餐
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(f"/t/{_table_token(app, 'E1')}")
    expect(page.locator('#table-banner')).to_contain_text('E1')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    expect(page.locator('#table-banner')).to_contain_text('E1')
    page.locator(f'#menuList .menu-row[data-id="{demo["示範雞塊"]}"] [data-action="add"]').click()
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    expect(page.locator('#table-checkout-hint')).to_contain_text('E1')
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    expect(page.locator('#preview-items')).to_contain_text('用餐後至櫃檯結帳')
    with page.expect_response(lambda r: r.url.endswith('/submit_order')) as resp:
        page.locator('[data-action="checkout"]').click()
    assert resp.value.ok
    page.locator('#receiptModal.show').wait_for()
    page.locator('#receiptModal [data-action="finish"]').click()
    page.locator('form[action$="/logout"] button').click()

    # 3. 櫃檯在 POS 結帳
    page.set_viewport_size({'width': 1366, 'height': 900})
    browser_staff_login(page, 'e2e_pos')
    page.goto('/pos')
    page.wait_for_load_state('networkidle')
    expect(page.locator('#topUnpaidTableCount')).not_to_have_text('0')
    page.locator('[data-action="openTables"]').click()
    card = page.locator('.pos-table-card', has_text='E1 桌')
    expect(card).to_be_visible()
    card.locator('.table-cash').fill('100')
    expect(card.locator('.table-change')).to_contain_text('找零 $30')
    page.once('dialog', lambda d: d.accept())
    card.locator('[data-action="settleTable"]').click()
    expect(page.locator('.pos-table-card', has_text='E1 桌')).to_have_count(0)
