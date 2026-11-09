"""修正小組 menu-admin-ui 的回歸測試 (後台菜單頁：編輯品項 / 加購條件 / 庫存基準 / 圖片裁切 / 套餐配餐 / 碳足跡)"""
import json
import re
import uuid

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import ComboOption, ComboOptionItem, MenuItem
from app.models.tenant import StaffAccount
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import E2E_STAFF_PASSWORD, browser_staff_login, default_tenant_id, jpeg_bytes, make_item, seed_demo

pytestmark = pytest.mark.e2e

STOCK_API = '**/api/admin/menu_items/*/stock'


@pytest.fixture(autouse=True)
def demo_data(app):
    seed_demo(app)


def _tag():
    return uuid.uuid4().hex[:6]


def _stock_from_db(app):
    """模擬「開啟編輯視窗時取目前庫存」的端點：回傳資料庫的現值"""
    def handler(route):
        item_id = int(route.request.url.rsplit('/', 2)[-2])
        with app.app_context():
            item = db.session.get(MenuItem, item_id)
            body = {'success': True, 'id': item.id, 'stock': item.stock or 0, 'total_stock': item.total_stock or 0,
                    'is_sold_out': bool(item.is_sold_out)}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
    return handler


def _open_menu(page, app):
    page.route(STOCK_API, _stock_from_db(app))
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert page.goto('/admin/menu').ok
    page.wait_for_load_state('networkidle')


def _wait_shown(page, modal_id):
    modal = page.locator(f'#{modal_id}')
    expect(modal).to_be_visible()
    page.wait_for_function(
        "id => getComputedStyle(document.querySelector('#' + id + ' .modal-dialog')).transform === 'none'", arg=modal_id)
    return modal


def _edit_item(page, item_id):
    page.locator(f'#menuItemTable [data-action="edit-item"][data-id="{item_id}"]').click()
    return _wait_shown(page, 'editItemModal')


def _get(app, item_id):
    with app.app_context():
        return db.session.get(MenuItem, item_id)


def test_edit_item_saves_when_addon_off_with_stale_target(page, app):
    """B-296：加購已關閉、殘留「需購買特定餐點」且目標已不存在，編輯價格仍可送出"""
    item_id = make_item(app, f'E2E殘值加購{_tag()}', price=100, can_be_add_on=False, add_on_price=0,
                        addon_trigger_type='item', addon_trigger_target='已刪除的紅茶')
    _open_menu(page, app)
    modal = _edit_item(page, item_id)
    expect(modal.locator('#edit-addon-rules')).to_be_hidden()
    modal.locator('#edit-price').fill('123')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    assert _get(app, item_id).price == 123


def test_edit_item_addon_on_keeps_stale_target(page, app):
    """B-296：加購開啟但目標已改名 / 刪除：下拉保留舊值 (標示已不存在)，可直接送出且不會被清掉"""
    item_id = make_item(app, f'E2E舊目標{_tag()}', price=100, can_be_add_on=True, add_on_price=30,
                        addon_trigger_type='item', addon_trigger_target='已改名的奶茶')
    _open_menu(page, app)
    modal = _edit_item(page, item_id)
    expect(modal.locator('#edit-addon-target')).to_have_value('已改名的奶茶')
    expect(modal.locator('#edit-addon-target option[value="已改名的奶茶"]')).to_contain_text('已不存在')
    modal.locator('#edit-price').fill('110')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    item = _get(app, item_id)
    assert (item.price, item.addon_trigger_type, item.addon_trigger_target) == (110, 'item', '已改名的奶茶')


def test_edit_item_turning_addon_off_without_target_still_submits(page, app):
    """B-296：同一視窗先開加購、選「需購買特定餐點」，未選目標就關閉加購，仍可儲存"""
    item_id = make_item(app, f'E2E開關加購{_tag()}', price=100)
    _open_menu(page, app)
    modal = _edit_item(page, item_id)
    modal.locator('#edit-addon-price').fill('20')
    expect(modal.locator('#edit-addon-rules')).to_be_visible()
    modal.locator('#edit-addon-type').select_option('item')
    expect(modal.locator('#edit-addon-target')).to_have_js_property('required', True)
    modal.locator('#edit-can-addon').uncheck()
    expect(modal.locator('#edit-addon-target')).to_have_js_property('required', False)
    modal.locator('#edit-price').fill('105')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    item = _get(app, item_id)
    assert (item.price, item.can_be_add_on) == (105, False)


def test_edit_item_uses_current_stock_as_baseline(page, app):
    """B-297：頁面載入後庫存被賣掉 (頁面沒有即時同步)，開啟編輯時以目前庫存為基準，輸入 5 就存成 5"""
    item_id = make_item(app, f'E2E庫存基準{_tag()}', price=100, stock=50, total_stock=50)
    _open_menu(page, app)
    with app.app_context():
        db.session.get(MenuItem, item_id).stock = 20  # 頁面開著期間賣出 30 份
        db.session.commit()
    modal = _edit_item(page, item_id)
    expect(modal.locator('#edit-stock')).to_have_value('20')
    expect(modal.locator('#edit-loaded-stock')).to_have_value('20')
    modal.locator('#edit-stock').fill('5')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    item = _get(app, item_id)
    assert (item.stock, bool(item.is_sold_out)) == (5, False)


def test_crop_cancel_does_not_upload_uncropped_image(page, app):
    """B-299：裁切視窗按「取消」後檔案欄位清空，儲存時不會上傳未裁切的原圖"""
    item_id = make_item(app, f'E2E取消裁切{_tag()}', price=100)
    _open_menu(page, app)
    modal = _edit_item(page, item_id)
    modal.locator('#edit-image-file').set_input_files(
        {'name': 'tall.jpg', 'mimeType': 'image/jpeg', 'buffer': jpeg_bytes(200, 360).getvalue()})
    crop = page.locator('#cropModal')
    expect(crop.locator('.cropper-container')).to_be_visible()
    _wait_shown(page, 'cropModal')
    crop.locator('.modal-footer [data-bs-dismiss="modal"]').click()
    expect(crop).to_be_hidden()
    # hidden.bs.modal 在遮罩淡出後才觸發
    page.wait_for_function("() => document.querySelector('#edit-image-file').files.length === 0")
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    assert not _get(app, item_id).image_path


def test_edit_combo_keeps_side_outside_side_categories(page, app):
    """B-298：固定配餐的分類已不在配餐分類清單 (飲料改成茶飲)，編輯套餐直接儲存不會刪掉該配餐"""
    tag = _tag()
    main_id = make_item(app, f'E2E主餐{tag}', price=150, category='主餐')
    tea_id = make_item(app, f'E2E茶飲{tag}', price=40, category='茶飲')
    with app.app_context():
        combo = ComboOption(tenant_id=default_tenant_id(app), main_item_id=main_id, name=f'E2E套餐{tag}',
                            additional_price=50, can_addon=True)
        combo.sides = [ComboOptionItem(tenant_id=combo.tenant_id, menu_item_id=tea_id, position=0)]
        db.session.add(combo)
        db.session.commit()
        combo_id = combo.id
    _open_menu(page, app)
    page.locator(f'#comboTable [data-action="edit-combo"][data-id="{combo_id}"]').click()
    modal = _wait_shown(page, 'editComboModal')
    expect(modal.locator('#edit-combo-side1')).to_have_value(str(tea_id))
    modal.locator('#edit-combo-price').fill('55')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    with app.app_context():
        combo = db.session.get(ComboOption, combo_id)
        assert combo.additional_price == 55
        assert [s.menu_item_id for s in combo.sides] == [tea_id]


def test_carbon_manual_input_after_unchecking_auto(page, app):
    """B-300：「進階」取消勾選「依配方」後碳足跡欄位可以輸入手動值並儲存"""
    item_id = make_item(app, f'E2E碳足跡{_tag()}', price=100, carbon_auto=True)
    _open_menu(page, app)
    page.locator(f'button[data-bs-target="#edit-{item_id}"]').click()
    grams = page.locator(f'#carbon-{item_id}')
    expect(grams).to_be_visible()
    expect(grams).to_have_js_property('readOnly', True)
    page.locator(f'#carbon-auto-{item_id}').uncheck()
    expect(grams).to_have_js_property('readOnly', False)
    grams.fill('350')
    page.locator(f'#edit-{item_id} button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    item = _get(app, item_id)
    assert (item.carbon_auto, item.carbon_grams) == (False, 350)


def test_edit_item_keeps_auto_restored_sold_out(page, app):
    """B-291：開啟編輯視窗後品項被自動恢復供應 (取消訂單回補)，沒動「售完」勾選就儲存時不會又被標回售完"""
    item_id = make_item(app, f'E2E售完基準{_tag()}', price=100, stock=10, total_stock=10, is_sold_out=True)
    _open_menu(page, app)
    modal = _edit_item(page, item_id)
    page.wait_for_load_state('networkidle')
    expect(modal.locator('#edit-sold-out')).to_be_checked()
    expect(modal.locator('#edit-loaded-sold-out')).to_have_value('1')
    with app.app_context():
        db.session.get(MenuItem, item_id).is_sold_out = False  # 編輯期間自動恢復供應
        db.session.commit()
    modal.locator('#edit-price').fill('120')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    item = _get(app, item_id)
    assert (item.price, bool(item.is_sold_out)) == (120, False)


def test_menu_form_inputs_have_length_and_range_limits(page, app):
    """B-283 / R2-101：名稱 / 分類 / 說明有 maxlength，金額與庫存有 max (與後端上限一致)"""
    _open_menu(page, app)
    expected = {
        '#add-name': ('maxlength', '100'), '#edit-name': ('maxlength', '100'),
        '#add-category': ('maxlength', '50'), '#edit-category': ('maxlength', '50'),
        '#combo-name': ('maxlength', '100'), '#edit-combo-name': ('maxlength', '100'),
        '#combo-desc': ('maxlength', '200'), '#edit-combo-desc': ('maxlength', '200'),
        '#add-price': ('max', '1000000'), '#edit-price': ('max', '1000000'),
        '#add-discount-price': ('max', '1000000'), '#edit-addon-price': ('max', '1000000'),
        '#combo-price': ('max', '1000000'), '#add-stock': ('max', '100000'), '#edit-total-stock': ('max', '100000'),
    }
    for selector, (attr, value) in expected.items():
        assert page.locator(selector).get_attribute(attr) == value, selector


def test_stale_addon_target_badge_is_highlighted(page, app):
    """R2-102：加購條件的目標品項已不存在時，清單徽章改用警示樣式並註明「目標已不存在」"""
    tag = _tag()
    stale_id = make_item(app, f'E2E失效加購{tag}', price=100, can_be_add_on=True, add_on_price=30,
                         addon_trigger_type='item', addon_trigger_target=f'已刪除品項{tag}')
    ok_id = make_item(app, f'E2E有效加購{tag}', price=100, can_be_add_on=True, add_on_price=30,
                      addon_trigger_type='item', addon_trigger_target='示範牛肉堡')
    _open_menu(page, app)
    stale = page.locator(f'#item-tags-{stale_id} .badge', has_text='可加購')
    expect(stale).to_contain_text('目標已不存在')
    expect(stale).to_have_class(re.compile(r'text-bg-warning'))
    ok = page.locator(f'#item-tags-{ok_id} .badge', has_text='可加購')
    expect(ok).not_to_contain_text('目標已不存在')
    expect(ok).not_to_have_class(re.compile(r'text-bg-warning'))


def test_edit_item_without_coupons_permission_hides_reward_block(page, app):
    """R2-115：只有「菜單」權限的員工看不到紅利商城設定，編輯視窗仍可正常開啟並儲存，紅利設定不變"""
    username = f'e2e_menu_only_{_tag()}'
    with app.app_context():
        acc = StaffAccount(tenant_id=default_tenant_id(app), username=username, role='staff', permissions='menu')
        acc.set_password(E2E_STAFF_PASSWORD)
        db.session.add(acc)
        db.session.commit()
    item_id = make_item(app, f'E2E紅利保留{_tag()}', price=100, is_reward=True, reward_points=30)
    page.route(STOCK_API, _stock_from_db(app))
    browser_staff_login(page, username)
    assert page.goto('/admin/menu').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#edit-is-reward')).to_have_count(0)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    modal = _edit_item(page, item_id)
    modal.locator('#edit-price').fill('130')
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_hidden()
    assert not errors
    item = _get(app, item_id)
    assert (item.price, item.is_reward, item.reward_points) == (130, True, 30)
