"""修正小組 pos-frontend-a-1 的回歸測試 (櫃檯 POS 前端：購物車 / 結帳快捷鍵 / 套餐視窗 / 掃碼)"""
import uuid

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import ComboOption, ComboOptionItem
from tests.support import browser_staff_login, default_tenant_id, make_item, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def demo_data(app):
    return seed_demo(app)


def open_pos(page):
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#posCartCount')).to_have_text('0')


def nugget(page):
    return page.locator('#posMenuGrid').get_by_text('示範雞塊', exact=True)


def set_discount(page):
    page.locator('[data-action="openOrderDiscount"]').click()
    expect(page.locator('#posDiscountModal')).to_be_visible()
    page.locator('[data-action="discountPreset"][data-type="percent"][data-value="10"]').click()
    page.locator('#posDiscountForm button[type="submit"]').click()
    expect(page.locator('#posDiscountModal')).to_be_hidden()
    expect(page.locator('#posManualRow')).to_be_visible()


def test_removing_last_line_clears_order_discount(app, page):
    """B-214：用每列的「×」刪掉最後一列等同清空，整單折扣不可帶到下一位顧客"""
    open_pos(page)
    nugget(page).click()
    set_discount(page)
    page.locator('#posCartList [data-action="cartQty"][data-delta="-999"]').first.click()
    expect(page.locator('#posCartCount')).to_have_text('0')
    nugget(page).click()
    expect(page.locator('#posCartCount')).to_have_text('1')
    expect(page.locator('#posManualRow')).to_be_hidden()
    expect(page.locator('#posFinalTotal')).to_have_text('70')


def test_f4_ignored_while_modal_open(app, page):
    """B-213：有視窗開著 (例如整單折扣) 時 F4 不可把購物車存成掛單"""
    open_pos(page)
    expect(page.locator('#holdCountBadge')).to_have_text('0')
    nugget(page).click()
    page.locator('[data-action="openOrderDiscount"]').click()
    expect(page.locator('#posDiscountModal')).to_be_visible()
    page.keyboard.press('F4')
    page.wait_for_timeout(200)
    expect(page.locator('#holdCountBadge')).to_have_text('0')
    expect(page.locator('#posCartCount')).to_have_text('1')


def test_unknown_barcode_enter_does_not_click_focused_button(app, page):
    """R2-075：焦點留在「清空」按鈕時掃到未登錄的條碼，Enter 不可觸發清空，並提示查無此條碼"""
    messages = []
    page.on('dialog', lambda d: (messages.append(d.message), d.accept()))
    open_pos(page)
    nugget(page).click()
    expect(page.locator('#posCartCount')).to_have_text('1')
    page.locator('[data-action="clearCart"]').focus()
    page.keyboard.type('ZZ0000UNKNOWN')
    page.keyboard.press('Enter')
    page.wait_for_timeout(300)
    expect(page.locator('#posCartCount')).to_have_text('1')
    assert any('查無此條碼' in m for m in messages), messages


def test_combo_card_double_click_adds_once(app, page):
    """R2-079：套餐視窗淡出期間連點兩下單點卡片，品項只加入一次"""
    tag = uuid.uuid4().hex[:6]
    main_id = make_item(app, f'連點主餐{tag}', price=90)
    side_id = make_item(app, f'連點配餐{tag}', price=30, category='副餐')
    with app.app_context():
        tid = default_tenant_id(app)
        combo = ComboOption(tenant_id=tid, main_item_id=main_id, name=f'連點套餐{tag}', additional_price=40, can_addon=True)
        combo.sides = [ComboOptionItem(tenant_id=tid, menu_item_id=side_id, position=0, customizable=False)]
        db.session.add(combo)
        db.session.commit()

    open_pos(page)
    page.locator('#posMenuGrid').get_by_text(f'連點主餐{tag}', exact=True).click()
    modal = page.locator('#posComboModal')
    expect(modal).to_be_visible()
    page.wait_for_function("document.getElementById('posComboModal').classList.contains('show')")
    page.wait_for_timeout(400)   # 開啟動畫結束
    page.evaluate("""() => {
        const card = document.querySelector('#posComboModal [data-action="chooseCombo"][data-combo-id="0"]');
        card.click();
        card.click();
    }""")
    expect(modal).to_be_hidden()
    page.wait_for_timeout(300)
    expect(page.locator('#posCartCount')).to_have_text('1')
    expect(page.locator('#posFinalTotal')).to_have_text('90')
