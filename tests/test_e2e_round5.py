"""端對端 (第五輪)：POS 暫停接單 → 點餐機橫幅與送單被擋、預估等候時間顯示、再點一次、售完即時更新、
POS 掃條碼、稽核紀錄頁、平台店家管理"""
import re
import uuid

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.menu import MenuItem
from app.services import intake_service, settings_service
from tests.support import browser_member_login, browser_staff_login, default_tenant_id, make_item, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def demo(app):
    ids = seed_demo(app)
    with app.app_context():
        tid = default_tenant_id(app)
        for name in ('示範薯條', '示範紅茶'):
            item = db.session.get(MenuItem, ids[name])
            item.stock, item.total_stock, item.is_sold_out = 50, 50, False
        settings_service.put('order_intake', {'paused': False, 'resume_at': '', 'max_orders_per_window': 0}, tid)
        settings_service.put('kds', {'eta_enabled': True, 'eta_base_minutes': 3, 'eta_default_prep': 5, 'eta_parallel': 3}, tid)
        db.session.commit()
    return ids


def _guest(page):
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')


def _add(page, item_id):
    page.locator(f'#menuList .menu-row[data-id="{item_id}"] [data-action="add"]').click()


def _confirm_modifiers_if_any(page):
    modal = page.locator('#modifierModal.show')
    try:
        modal.wait_for(timeout=1500)
        page.locator('[data-action="confirm-modifiers"]').click()
        page.locator('#modifierModal').wait_for(state='hidden')
    except Exception:  # noqa: BLE001  沒有客製化視窗
        pass


def test_pos_pause_toggle_blocks_kiosk_then_resume(app, page, demo):
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#posIntakeBadge')).to_have_text('接單中')
    page.locator('[data-action="openIntake"]').click()
    page.locator('#posIntakeModal.show').wait_for()
    page.locator('[data-action="intakePause"][data-minutes="15"]').click()
    expect(page.locator('#posIntakeBadge')).to_have_text('已暫停')
    expect(page.locator('#posIntakeState')).to_contain_text('自動恢復')
    with app.app_context():
        with app.test_request_context('/'):
            assert intake_service.is_paused(default_tenant_id(app)) is True
    # 點餐機：橫幅 + 送單被擋
    page.locator('#posIntakeModal .btn-close').click()
    _guest(page)
    expect(page.locator('#intake-paused-banner')).to_be_visible()
    _add(page, demo['示範薯條'])
    _confirm_modifiers_if_any(page)
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    with page.expect_response(lambda r: r.url.endswith('/submit_order')) as resp:
        page.locator('[data-action="checkout"]').click()
    assert resp.value.status == 409 and resp.value.json().get('intake_paused') is True
    page.problems[:] = [p for p in page.problems if '409 (CONFLICT)' not in p]   # 被擋下的送單是預期的
    # 恢復 (員工登入仍有效)
    page.locator('#confirmModal .btn-secondary').click()
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('[data-action="openIntake"]').click()
    page.locator('#posIntakeModal.show').wait_for()
    page.locator('[data-action="intakeResume"]').click()
    expect(page.locator('#posIntakeBadge')).to_have_text('接單中')
    with app.app_context():
        with app.test_request_context('/'):
            assert intake_service.is_paused(default_tenant_id(app)) is False


def test_kiosk_eta_shown_in_cart_preview_receipt_and_reorder(app, page, demo):
    browser_member_login(page)
    page.goto('/')
    page.wait_for_load_state('networkidle')
    _add(page, demo['示範薯條'])
    _confirm_modifiers_if_any(page)
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    expect(page.locator('#cart-eta-row')).to_be_visible()
    minutes = int(page.locator('#cart-eta').inner_text())
    assert minutes >= 3 + 5
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    expect(page.locator('#preview-eta-row')).to_be_visible()
    with page.expect_request(lambda r: r.url.endswith('/submit_order') and r.method == 'POST') as req:
        page.locator('[data-action="checkout"]').click()
    page.locator('#receiptModal.show').wait_for()
    expect(page.locator('#receipt-eta-row')).to_be_visible()
    assert re.match(r'^\d\d:\d\d$', page.locator('#receipt-eta').inner_text())
    client_order_id = req.value.post_data_json['client_order_id']
    # 我的訂單：預計時間徽章 + 再點一次
    page.locator('[data-action="finish"]').click()
    page.wait_for_load_state('networkidle')
    page.locator('[data-action="my-orders"]').click()
    page.locator('#historyModal.show').wait_for()
    first = page.locator('#history-modal-body .card').first
    first.wait_for()
    expect(first.locator('.order-eta')).to_be_visible()
    first.locator('[data-action="reorder"]').click()   # 加入成功的 alert 由 page fixture 自動關閉
    page.locator('#historyModal').wait_for(state='hidden')
    expect(page.locator('#cart-count')).to_have_text('1')
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    assert '示範薯條' in page.locator('#cart-items').inner_text()
    assert client_order_id


def test_kiosk_sold_out_updates_without_reload(app, page, demo):
    _guest(page)
    row = page.locator(f'#menuList .menu-row[data-id="{demo["示範紅茶"]}"]')
    expect(row.locator('[data-action="add"]')).to_be_visible()
    with app.app_context():
        db.session.get(MenuItem, demo['示範紅茶']).is_sold_out = True
        db.session.commit()
    # 直接觸發輪詢 (正式每 30 秒；切回分頁也會觸發)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(row.locator('.menu-card')).to_have_class(re.compile('menu-card-sold-out'), timeout=5000)
    expect(row.locator('button[disabled]')).to_be_visible()
    with app.app_context():
        db.session.get(MenuItem, demo['示範紅茶']).is_sold_out = False
        db.session.commit()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(row.locator('[data-action="add"]')).to_be_visible(timeout=5000)


def test_pos_barcode_scan_adds_item(app, page, demo):
    code = f'E2E{uuid.uuid4().hex[:8].upper()}'
    item = make_item(app, f'條碼餐 {code}', price=45, external_code=code)
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    # 1. 搜尋框輸入代碼 + Enter
    page.fill('#posSearchInput', code)
    page.keyboard.press('Enter')
    expect(page.locator('#posCartCount')).to_have_text('1')
    assert f'條碼餐 {code}' in page.locator('#posCartList').inner_text()
    # 2. 焦點不在輸入框：掃描槍快速鍵入 + Enter
    page.locator('body').click(position={'x': 5, 'y': 5})
    page.keyboard.type(code, delay=10)
    page.keyboard.press('Enter')
    expect(page.locator('#posCartCount')).to_have_text('2')
    assert item


def test_audit_page_filter_and_tenant_management(app, page, demo):
    browser_staff_login(page, 'e2e_manager')
    assert page.goto('/admin/audit').ok
    expect(page.locator('#auditLogTable')).to_be_visible()
    page.select_option('#af-staff', 'e2e_pos')
    page.locator('#auditFilterForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    assert 'staff=e2e_pos' in page.url
    expect(page.locator('#auditSummary')).to_contain_text('共')
    assert page.goto('/admin/audit?tab=logins').ok
    expect(page.locator('#loginLogTable')).to_contain_text('e2e_manager')
    # 店家管理只有平台管理者 (預設店家負責人)：店長被擋 (403 是預期的)
    resp = page.goto('/admin/tenants')
    assert resp.status == 403 or page.locator('#tenantTable').count() == 0
    page.problems[:] = [p for p in page.problems if '403 (FORBIDDEN)' not in p]
