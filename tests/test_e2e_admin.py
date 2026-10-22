"""端對端：後台管理 (登入、菜單/優惠券 CRUD、儀表板、訂單狀態、員工帳號、店家註冊、XSS 顯示為純文字)"""
import re
import uuid
from datetime import datetime

import pytest
from playwright.sync_api import expect

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import browser_staff_login, default_tenant_id, seed_demo

pytestmark = pytest.mark.e2e

XSS_ITEM = "O'Brien 的「特製」<script>alert(1)</script>"
XSS_MEMBER = "示範會員 </script><b>王'小明'"
XSS_COMBO = "A套餐(薯條+紅茶) <i>'含飲料'</i>"


@pytest.fixture(autouse=True)
def demo_data(app):
    seed_demo(app)


def _suffix():
    return uuid.uuid4().hex[:6]


def _login(page):
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    expect(page.locator('#adminTab')).to_be_visible()


def _open_tab(page, key):
    page.click(f'#{key}-tab')
    expect(page.locator(f'#{key}-pane')).to_be_visible()


def _close_modal(page, modal_id):
    """Bootstrap 在淡入動畫期間會忽略關閉，先等動畫結束 (對話框 transform 回到 none) 再按關閉"""
    modal = page.locator(f'#{modal_id}')
    expect(modal).to_be_visible()
    page.wait_for_function(
        "id => getComputedStyle(document.querySelector('#' + id + ' .modal-dialog')).transform === 'none'", arg=modal_id)
    page.wait_for_timeout(50)
    modal.locator('.btn-close').click()
    expect(modal).to_be_hidden()


def test_login_wrong_password_then_success(page):
    page.goto('/admin')
    page.fill('input[name="username"]', ADMIN_USERNAME)
    page.fill('input[name="password"]', 'definitely-wrong')
    page.click('button[type="submit"]')
    expect(page.locator('.flash-stack')).to_contain_text('帳號或密碼錯誤')
    page.problems.clear()  # 401 回應本身會在主控台記錄一筆資源載入錯誤，屬預期行為

    page.fill('input[name="username"]', ADMIN_USERNAME)
    page.fill('input[name="password"]', ADMIN_PASSWORD)
    page.click('button[type="submit"]')
    expect(page.locator('#adminTab')).to_be_visible()
    expect(page.locator('#live-clock')).to_have_text(re.compile(r'^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$'))
    # 平台管理者看得到整庫備份/還原分頁
    expect(page.locator('#pane-export-zip')).to_have_count(1)
    expect(page.locator('#pane-restore-zip')).to_have_count(1)


def test_add_menu_item_via_form(page, app):
    from app.models.menu import MenuItem
    name = f'E2E 後台新品 {_suffix()}'
    _login(page)
    _open_tab(page, 'menu')
    page.click('button[data-bs-target="#addItemModal"]')
    modal = page.locator('#addItemModal')
    expect(modal).to_be_visible()
    modal.locator('#add-name').fill(name)
    modal.locator('#add-category').fill('主餐')
    modal.locator('#add-price').fill('120')
    modal.locator('#add-stock').fill('15')
    modal.locator('#add-description').fill('<b>描述不是粗體</b>')
    # 加購價會自動打開「開放作為加購品」並顯示加購條件；選擇指定分類
    modal.locator('#add-addon-price').fill('30')
    expect(modal.locator('#add-can-addon')).to_be_checked()
    modal.locator('#add-addon-type').select_option('category')
    modal.locator('#add-addon-target').select_option('飲料')
    modal.locator('button[type="submit"]').click()

    row = page.locator('#menuItemTable tbody tr', has_text=name)
    expect(row).to_be_visible()  # 送出後導回 ?tab=menu 並自動切到菜單分頁
    expect(row).to_contain_text('<b>描述不是粗體</b>')
    expect(row).to_contain_text('[限分類:飲料]')
    with app.app_context():
        item = MenuItem.query.filter_by(name=name).one()
        assert (item.price, item.stock, item.can_be_add_on, item.add_on_price) == (120, 15, True, 30)
        assert (item.addon_trigger_type, item.addon_trigger_target) == ('category', '飲料')

    # 編輯視窗帶入資料庫的現值，修改價格後儲存
    row.locator('[data-action="edit-item"]').click()
    edit = page.locator('#editItemModal')
    expect(edit.locator('#edit-name')).to_have_value(name)
    expect(edit.locator('#edit-addon-target')).to_have_value('飲料')
    edit.locator('#edit-price').fill('130')
    edit.locator('button[type="submit"]').click()
    expect(page.locator('#menuItemTable tbody tr', has_text=name)).to_contain_text('$130')
    with app.app_context():
        assert MenuItem.query.filter_by(name=name).one().price == 130


def test_menu_image_crop_upload_and_delete(page, app):
    """選圖後以 Cropper 裁切 1:1、上傳後顯示於列表；最後以刪除表單 (data-confirm) 刪除"""
    import os

    from app.models.menu import MenuItem
    from tests.support import jpeg_bytes
    menu_dir = app.config['UPLOAD_FOLDER_MENU']

    name = f'E2E 裁切圖片 {_suffix()}'
    _login(page)
    _open_tab(page, 'menu')
    page.click('button[data-bs-target="#addItemModal"]')
    modal = page.locator('#addItemModal')
    modal.locator('#add-name').fill(name)
    modal.locator('#add-category').fill('點心')
    modal.locator('#add-price').fill('45')
    modal.locator('#add-image-file').set_input_files(
        {'name': 'photo.jpg', 'mimeType': 'image/jpeg', 'buffer': jpeg_bytes(320, 200).getvalue()})
    crop = page.locator('#cropModal')
    expect(crop.locator('.cropper-container')).to_be_visible()
    crop.locator('[data-action="confirm-crop"]').click()
    expect(crop).to_be_hidden()
    expect(modal.locator('#add-preview-img')).to_be_visible()
    assert modal.locator('#add-image-file').evaluate('el => el.files[0].name').startswith('cropped_')
    modal.locator('button[type="submit"]').click()

    row = page.locator('#menuItemTable tbody tr', has_text=name)
    expect(row).to_be_visible()
    with app.app_context():
        item = MenuItem.query.filter_by(name=name).one()
        image_path, item_id = item.image_path, item.id
    assert image_path and os.path.isfile(os.path.join(menu_dir, image_path))
    from PIL import Image
    with Image.open(os.path.join(menu_dir, image_path)) as im:
        assert im.size[0] == im.size[1], '裁切後應為 1:1'
    img = row.locator('img')
    expect(img).to_have_attribute('src', re.compile(re.escape(image_path) + '$'))
    for _ in range(50):  # 圖片確實由 /media/menu 載入 (不是破圖)
        if img.evaluate('el => el.complete && el.naturalWidth > 0'):
            break
        page.wait_for_timeout(100)
    else:
        raise AssertionError('餐點圖片載入失敗')

    row.locator('form[data-confirm] button[type="submit"]').click()
    expect(page.locator('#menuItemTable tbody tr', has_text=name)).to_have_count(0)
    with app.app_context():
        from app.extensions import db
        assert db.session.get(MenuItem, item_id) is None


def test_add_item_client_validation_blocks_bad_addon_price(page, app):
    from app.models.menu import MenuItem
    name = f'E2E 加購防呆 {_suffix()}'
    _login(page)
    _open_tab(page, 'menu')
    page.click('button[data-bs-target="#addItemModal"]')
    modal = page.locator('#addItemModal')
    modal.locator('#add-name').fill(name)
    modal.locator('#add-category').fill('點心')
    modal.locator('#add-price').fill('50')
    modal.locator('#add-addon-price').fill('80')  # 加購價高於原價：前端攔截，不送出
    messages = []
    page.once('dialog', lambda d: messages.append(d.message))
    modal.locator('button[type="submit"]').click()
    expect(modal).to_be_visible()
    assert messages and '加購專屬價' in messages[0]
    with app.app_context():
        assert MenuItem.query.filter_by(name=name).first() is None


def test_add_coupon_via_form(page, app):
    from app.models.coupon import Coupon
    code = f'E2E{_suffix().upper()}'
    title = f"E2E 85折券 <i>'{code}'</i>"
    _login(page)
    _open_tab(page, 'rewards')
    page.click('button[data-bs-target="#createCouponModal"]')
    modal = page.locator('#createCouponModal')
    expect(modal).to_be_visible()
    modal.locator('#coupon-title').fill(title)
    modal.locator('#coupon-code').fill(code.lower())
    modal.locator('#coupon-disc-type').select_option('percent')
    modal.locator('#coupon-disc-val').fill('85')
    expect(modal.locator('#coupon-disc-hint')).to_contain_text('85 折')
    modal.locator('#coupon-min-spend').fill('100')
    modal.locator('#coupon-per-user-limit').fill('2')
    modal.locator('#coupon-reward-points').fill('40')
    modal.locator('button[type="submit"]').click()

    row = page.locator('#couponTable tbody tr', has_text=code)
    expect(row).to_be_visible()
    expect(row).to_contain_text(title)
    expect(row).to_contain_text('85 折')
    with app.app_context():
        cp = Coupon.query.filter_by(code=code).one()
        assert (cp.discount_type, cp.discount_value, cp.min_spend, cp.reward_points, cp.per_user_limit) == ('percent', 85, 100, 40, 2)

    # 編輯視窗帶入現值 (含折數提示)
    row.locator('[data-action="edit-coupon"]').click()
    edit = page.locator('#editCouponModal')
    expect(edit.locator('#edit-coupon-title')).to_have_value(title)
    expect(edit.locator('#edit-coupon-disc-type')).to_have_value('percent')
    expect(edit.locator('#edit-coupon-disc-hint')).to_contain_text('85 折')
    expect(edit.locator('#edit-coupon-per-user-limit')).to_have_value('2')


def test_dashboard_analytics_and_order_status(page, app):
    from app.extensions import db
    from app.models.order import Order, OrderItem
    name = f'<img src=x onerror=alert(2)>熱賣{_suffix()}'
    with app.app_context():
        order = Order(tenant_id=default_tenant_id(app), table_number='E2E 後台', pickup_number=777, total_price=9900,
                      payment_method='Cash', order_type='外帶', note="<b>少冰</b>", status='Pending', created_at=datetime.now())
        order.items = [OrderItem(tenant_id=order.tenant_id, item_name=name, price=100, quantity=99)]
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    _login(page)
    # 實時訂單看板：訂單卡片以純文字顯示品名與備註
    card = page.locator('#admin-orders-grid .card', has_text=name)
    expect(card).to_be_visible()
    expect(card).to_contain_text('備註：<b>少冰</b>')
    expect(card).to_contain_text(f'單號 #{order_id}')

    # 營運分析：熱門排行 (stat-top-items) 有資料
    _open_tab(page, 'analytics')
    top = page.locator('#stat-top-items')
    expect(top).to_be_visible()
    expect(top).to_contain_text(name)
    expect(top).to_contain_text('已售 99 份')
    expect(page.locator('#stat-today-orders')).not_to_have_text('0')

    # 調整排行筆數 (Enter 送出) 後停留在營運分析分頁
    page.fill('#rankingLimitInput', '5')
    page.press('#rankingLimitInput', 'Enter')
    page.wait_for_url(re.compile(r'limit=5'))
    expect(page.locator('#analytics-pane')).to_be_visible()
    expect(page.locator('#stat-top-items')).to_contain_text(name)

    # 在看板上把訂單改為「已出餐」
    _open_tab(page, 'orders')
    card = page.locator('#admin-orders-grid .card', has_text=name)
    card.locator('select[data-action="order-status"]').select_option('Completed')
    expect(page.locator('#admin-orders-grid .card', has_text=name)).to_have_class(re.compile('border-success'))
    with app.app_context():
        assert db.session.get(Order, order_id).status == 'Completed'


def test_xss_names_render_as_text(page):
    _login(page)
    _open_tab(page, 'menu')
    item_name = page.locator('#menuItemTable .item-name', has_text=XSS_ITEM)
    expect(item_name).to_be_visible()
    expect(item_name).to_have_text(XSS_ITEM)
    expect(page.locator('#comboTable')).to_contain_text(XSS_COMBO)
    # 編輯視窗的輸入框帶入原始字串
    page.locator('#menuItemTable tbody tr', has_text=XSS_ITEM).locator('[data-action="edit-item"]').click()
    expect(page.locator('#edit-name')).to_have_value(XSS_ITEM)
    _close_modal(page, 'editItemModal')

    # 套餐編輯視窗：配餐依 sides 帶入
    page.locator('#comboTable tbody tr', has_text=XSS_COMBO).locator('[data-action="edit-combo"]').click()
    expect(page.locator('#edit-combo-name')).to_have_value(XSS_COMBO)
    expect(page.locator('#edit-combo-side1 option:checked')).to_contain_text('示範薯條')
    expect(page.locator('#edit-combo-side2 option:checked')).to_contain_text('示範紅茶')
    _close_modal(page, 'editComboModal')

    _open_tab(page, 'users')
    member = page.locator('#memberTable .member-name', has_text=XSS_MEMBER)
    expect(member).to_be_visible()
    expect(member).to_have_text(XSS_MEMBER)
    row = page.locator('#memberTable tbody tr', has_text=XSS_MEMBER)
    row.locator('[data-action="user-coupons"]').click()
    expect(page.locator('#userCouponsModalTitle')).to_have_text(f'{XSS_MEMBER} 的專屬票券管理')
    _close_modal(page, 'userCouponsModal')
    row.locator('[data-action="edit-user"]').click()
    expect(page.locator('#edit-user-name')).to_have_value(XSS_MEMBER)
    expect(page.locator('#edit-user-pin')).to_have_value('')


def test_staff_accounts_create(page, app):
    from app.models.tenant import StaffAccount
    username = f'e2e_staff_{_suffix()}'
    _login(page)
    page.click('a[href="/admin/staff"]')
    expect(page.locator('#createStaffForm')).to_be_visible()
    page.fill('#staff-username', username)
    page.fill('#staff-password', 'StaffPass!2026')
    page.select_option('#staff-role', 'kitchen')
    page.click('#createStaffForm button[type="submit"]')
    row = page.locator('#staffTable tbody tr', has_text=username)
    expect(row).to_be_visible()
    expect(row).to_contain_text('廚房人員')
    expect(row).to_contain_text('啟用中')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).one()
        assert (acc.role, acc.is_active) == ('kitchen', True)

    # 停用帳號
    row.locator('button', has_text='停用').click()
    expect(page.locator('#staffTable tbody tr', has_text=username)).to_contain_text('已停用')

    # 密碼太短：後端以 flash 提示
    page.fill('#staff-username', f'{username}_x')
    page.fill('#staff-password', '1234567890')
    page.evaluate("document.getElementById('staff-password').value = '123'")
    page.click('#createStaffForm button[type="submit"]')
    expect(page.locator('.flash-stack')).to_contain_text('密碼至少 10 個字元')


def test_tenant_register_page(page, app):
    from app.models.tenant import Tenant
    slug = f'e2e-shop-{_suffix()}'
    page.goto('/tenant/register')
    expect(page.locator('#tenantRegisterForm')).to_be_visible()
    page.fill('#shop-name', f"E2E <b>新店</b> {slug}")
    page.fill('#shop-slug', slug)
    page.fill('#owner-username', f'owner_{slug}')
    page.fill('#owner-password', 'OwnerPass!2026')
    page.click('#tenantRegisterForm button[type="submit"]')
    expect(page.locator('#adminTab')).to_be_visible()   # 註冊後直接登入新店家的後台
    # 新店家不是平台管理者：看不到整庫備份/還原
    expect(page.locator('#pane-export-zip')).to_have_count(0)
    expect(page.locator('#pane-restore-zip')).to_have_count(0)
    with app.app_context():
        assert Tenant.query.filter_by(slug=slug).one().name == f"E2E <b>新店</b> {slug}"
