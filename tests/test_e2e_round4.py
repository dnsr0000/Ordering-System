"""端對端 (第四輪)：費用登錄 → 損益表、API 說明文件的線上試打、點餐機斷線暫存補送、團體預購"""
import re
import uuid
from datetime import date

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.expense import Expense
from app.models.integration import ApiKey
from app.models.order import Order
from tests.support import browser_staff_login, default_tenant_id, make_item, seed_demo

pytestmark = pytest.mark.e2e


def test_admin_expense_form_then_pnl_statement(app, page):
    seed_demo(app)
    name = f'E2E房租{uuid.uuid4().hex[:4]}'
    browser_staff_login(page, 'e2e_manager')
    assert page.goto('/admin/expenses').ok
    form = page.locator('#expenseForm')
    form.locator('input[name="spent_on"]').fill(date.today().isoformat())
    form.locator('select[name="category"]').select_option('rent')
    form.locator('input[name="name"]').fill(name)
    form.locator('input[name="amount"]').fill('12345')
    form.locator('input[name="is_fixed"]').check()
    form.locator('button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#expenseTable')).to_contain_text(name)
    expect(page.locator('#expenseTable')).to_contain_text('12,345')
    with app.app_context():
        assert Expense.query.filter_by(name=name).one().is_fixed is True
    assert page.goto('/admin/pnl').ok
    expect(page.locator('#pnlTable [data-pnl="expense_total"]')).to_contain_text('12,345')
    expect(page.locator('#pnlTable')).to_contain_text('租金')
    with app.app_context():
        Expense.query.filter_by(name=name).delete()
        db.session.commit()


def _pick(page, text):
    value = page.locator('#tryEndpoint option', has_text=text).get_attribute('value')
    page.locator('#tryEndpoint').select_option(value)


def test_developer_docs_try_it_with_api_key(app, page):
    seed_demo(app)
    with app.app_context():
        key = ApiKey(tenant_id=default_tenant_id(app), name='E2E 文件試打', scopes='menu:read,orders:read')
        raw = key.issue()
        db.session.add(key)
        db.session.commit()
        kid = key.id
    assert page.goto('/developers').ok
    expect(page.locator('#endpointList .api-endpoint')).to_have_count(7)
    page.fill('#tryKey', raw)
    _pick(page, 'GET /api/v1/menu —')
    page.locator('#tryItSend').click()
    expect(page.locator('#tryStatus')).to_contain_text('HTTP 200')
    expect(page.locator('#tryResult')).to_contain_text('示範牛肉堡')
    # 沒有權限的端點：403
    _pick(page, '每日營業統計')
    page.locator('#tryItSend').click()
    expect(page.locator('#tryStatus')).to_contain_text('HTTP 403')
    expect(page.locator('#tryResult')).to_contain_text('reports:read')
    # 路徑參數：查詢不存在的訂單 → 404
    _pick(page, '單筆訂單')
    page.fill('#tryParam_uuid', 'no-such-order')
    page.locator('#tryItSend').click()
    expect(page.locator('#tryStatus')).to_contain_text('/api/v1/orders/no-such-order → HTTP 404')
    # 上面刻意呼叫的 403 / 404 會在瀏覽器主控台留下「Failed to load resource」，不算頁面錯誤
    page.problems[:] = [p for p in page.problems if not re.search(r'status of (403|404)', p)]
    with app.app_context():
        db.session.delete(db.session.get(ApiKey, kid))
        db.session.commit()


def test_kiosk_offline_queue_then_replay(app, page):
    seed_demo(app)
    item = make_item(app, f'E2E斷線餐{uuid.uuid4().hex[:4]}', price=65, stock=20)
    page.goto('/?shop=default')
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    page.locator(f'#menuList .menu-row[data-id="{item}"] [data-action="add"]').click()
    page.locator('[data-bs-target="#cart-sidebar"]').click()
    page.locator('#cart-sidebar.show').wait_for()
    page.locator('[data-action="preview"]').click()
    page.locator('#confirmModal.show').wait_for()
    # 門市主機連不上：結帳與補送都失敗 (點餐機每 15 秒會自動補送一次，不擋住補送的話在較慢的機器上會在檢查前就送出)
    page.route('**/submit_order', lambda route: route.abort())
    page.route('**/api/kiosk/offline_order', lambda route: route.abort())
    page.locator('[data-action="checkout"]').click()
    page.locator('#offlineReceiptModal.show').wait_for()
    code = page.locator('#offlineReceiptCode').inner_text()
    assert re.fullmatch(r'[0-9A-F]{4}', code)
    page.locator('#offlineReceiptModal [data-bs-dismiss="modal"]').click()
    expect(page.locator('#offlineQueueBadge')).to_contain_text('1 筆待補送')
    expect(page.locator('#cart-count')).to_have_text('0')
    with app.app_context():
        assert Order.query.filter(Order.note.like(f'[斷線暫存 {code}%')).count() == 0
    page.evaluate('KioskOffline.flush()')   # 仍連不上時補送失敗：維持待補送 (不會變成失敗或遺失)
    expect(page.locator('#offlineQueueBadge')).to_contain_text('1 筆待補送')
    page.unroute('**/submit_order')
    page.unroute('**/api/kiosk/offline_order')
    page.evaluate('KioskOffline.flush()')
    expect(page.locator('#offlineQueueBadge')).to_contain_text('已補送 1 筆')
    with app.app_context():
        order = Order.query.filter(Order.note.like(f'[斷線暫存 {code}%')).one()
        assert order.user_id is None and order.total_price == 65
        pickup = order.pickup_number
    page.locator('#offlineQueueBadge').click()
    expect(page.locator('#offlineQueueModal')).to_contain_text(f'取餐號 #{pickup:03d}')
    page.locator('#offlineQueueClear').click()
    expect(page.locator('#offlineQueueBadge')).to_be_hidden()
    page.problems[:] = [p for p in page.problems if 'ERR_FAILED' not in p]


def test_group_order_browser_flow(app, page):
    from app.services import settings_service
    seed_demo(app)
    tid = default_tenant_id(app)
    with app.test_request_context('/'):
        from app.models.tenant import Tenant
        from app.tenant_context import _use_tenant
        _use_tenant(db.session.get(Tenant, tid), remember=False)
        old = dict(settings_service.get('group_order', tid))
        settings_service.put('group_order', dict(old, enabled=True, min_lead_minutes=60), tenant_id=tid)
        db.session.commit()
    item = make_item(app, f'E2E團購便當{uuid.uuid4().hex[:4]}', price=110, stock=30)
    try:
        page.goto('/?shop=default')
        page.locator('#groupOrderLink').click()
        page.wait_for_load_state('networkidle')
        page.fill('#gTitle', 'E2E 團購')
        page.fill('#gName', '主揪')
        page.fill('#gPhone', '0912000111')
        page.locator('#groupNewForm button[type="submit"]').click()
        page.wait_for_load_state('networkidle')
        share = page.locator('#groupShareUrl').inner_text()
        page.goto(share)
        page.fill('#geName', '小明')
        page.locator(f'.group-item[data-item-id="{item}"] input[name="qty_{item}"]').fill('2')
        page.locator('#groupEntrySubmit').click()
        page.wait_for_load_state('networkidle')
        expect(page.locator('.my-entry')).to_contain_text('小明')
        expect(page.locator('#groupTotals')).to_contain_text('2 份')
        page.get_by_role('link', name='管理團購').click()
        page.wait_for_load_state('networkidle')
        expect(page.locator('#groupTotal')).to_contain_text('$220')
        page.locator('#groupSubmitBtn').click()
        page.wait_for_load_state('networkidle')
        expect(page.locator('#groupSubmitted')).to_contain_text('取餐號')
    finally:
        with app.test_request_context('/'):
            from app.models.tenant import Tenant
            from app.tenant_context import _use_tenant
            _use_tenant(db.session.get(Tenant, tid), remember=False)
            settings_service.put('group_order', old, tenant_id=tid)
            db.session.commit()
