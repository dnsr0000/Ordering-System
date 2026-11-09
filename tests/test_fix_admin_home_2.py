"""修正小組 admin-home-2 的回歸測試 (後台總覽的權限過濾、待處理事項連結、側欄項目、共用頂欄店名、作廢確認視窗)"""
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from app.extensions import db
from app.models.payment import Refund
from app.models.tenant import StaffAccount, Tenant
from tests.support import default_tenant_id, login_staff, tenant_request


def _nav_labels(html):
    nav = html.split('id="adminSidebar"')[1].split('</nav>')[0]
    return re.findall(r'class="admin-nav-link[^"]*"[^>]*>\s*<i class="bi [^"]+"></i><span>([^<]+)</span>', nav)


@contextmanager
def _staff(app, role, permissions):
    """建立一個帶指定頁面權限的新員工並登入；結束時停用帳號 (不影響其他測試的共用帳號)"""
    username = f'ah2_{role}_{uuid.uuid4().hex[:6]}'
    client = login_staff(app, username, role)   # 密碼為 support.login_staff 建立帳號時的 'StaffPass!2026'
    client.staff_username = username
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).first()
        acc.permissions = permissions
        db.session.commit()
    try:
        yield client
    finally:
        with app.app_context():
            acc = StaffAccount.query.filter_by(username=username).first()
            acc.permissions = ''
            acc.is_active = False
            db.session.commit()


def test_b310_home_hides_revenue_without_reports_permission(app, admin_client):
    with _staff(app, 'staff', 'menu') as c:
        html = c.get('/admin').get_data(as_text=True)
        assert 'id="home-pane"' in html and 'id="stat-today-orders"' in html
        for gone in ('stat-today-revenue', 'stat-avg-order-value', 'stat-ai-advice'):
            assert f'id="{gone}"' not in html, gone
        assert '今日營收' not in html
    html = admin_client.get('/admin').get_data(as_text=True)
    assert all(f'id="{k}"' in html for k in ('stat-today-revenue', 'stat-avg-order-value', 'stat-ai-advice'))


def test_b323_quick_links_follow_role(app):
    with _staff(app, 'kitchen', 'menu') as c:
        quick = c.get('/admin').get_data(as_text=True).split('id="homeQuickLinks"')[1]
        assert '/admin/dispatch' not in quick, '廚房沒有外送派車權限，常用功能不顯示'
    with _staff(app, 'staff', 'menu') as c:
        quick = c.get('/admin').get_data(as_text=True).split('id="homeQuickLinks"')[1]
        assert '/admin/dispatch' in quick


def test_b311_b323_refund_pending_link_covers_failed_and_old_records(app, admin_client):
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        r = Refund(tenant_id=tid, kind='partial', amount=50, method='Line Pay', status='failed')
        db.session.add(r)
        db.session.commit()
        rid = r.id
    try:
        pending = admin_client.get('/admin').get_data(as_text=True).split('id="homePending"')[1].split('id="homeTopItems"')[0]
        assert re.search(r'/admin/refunds\?status=manual(,|%2C)failed&amp;start=2000-01-01', pending), pending
        with _staff(app, 'staff', 'coupons') as c:
            pending = c.get('/admin').get_data(as_text=True).split('id="homePending"')[1].split('id="homeTopItems"')[0]
            assert '/admin/refunds' not in pending, '櫃檯打不開退款頁，不列出此項'
    finally:
        with tenant_request(app, tid):
            db.session.delete(db.session.get(Refund, rid))
            db.session.commit()


def test_b319_coupon_permission_shows_marketing_pages(app):
    with _staff(app, 'staff', 'coupons') as c:
        labels = _nav_labels(c.get('/admin').get_data(as_text=True))
        for must in ('集點 / 寄杯', '禮物卡', '分群與自動行銷', '促銷活動'):
            assert must in labels, must
    with _staff(app, 'staff', 'menu') as c:
        labels = _nav_labels(c.get('/admin').get_data(as_text=True))
        assert '禮物卡' not in labels and '分群與自動行銷' not in labels


def test_b320_hub_hides_store_only_channels(app, admin_client, monkeypatch):
    labels = _nav_labels(admin_client.get('/admin').get_data(as_text=True))
    assert '訂位 / 候位' in labels and '團體預購' in labels, '單機 / 門市照常顯示'
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    labels = _nav_labels(admin_client.get('/admin').get_data(as_text=True))
    assert '訂位 / 候位' not in labels and '團體預購' not in labels


def test_r2_016_r2_155_hub_hides_kitchen_pickup_and_timeclock(app, admin_client, monkeypatch):
    store_only = ('廚房 KDS', '取餐核銷', '叫號看板', '打卡與排班', '可排時段', '排班建議', '自動排班')
    labels = _nav_labels(admin_client.get('/admin/security').get_data(as_text=True))
    assert all(x in labels for x in store_only), '單機 / 門市照常顯示'
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    labels = _nav_labels(admin_client.get('/admin/security').get_data(as_text=True))
    assert not [x for x in store_only if x in labels]


def test_b304_staff_sees_group_orders_nav(app):
    with _staff(app, 'staff', 'menu') as c:
        assert '團體預購' in _nav_labels(c.get('/admin/security').get_data(as_text=True))
    with _staff(app, 'kitchen', 'menu') as c:
        assert '團體預購' not in _nav_labels(c.get('/admin/security').get_data(as_text=True))


def test_r2_138_live_board_shows_refunded_badge():
    js = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'admin.js').read_text(encoding='utf-8')
    assert 'o.refunded_amount' in js and '已退 ' in js


def test_b322_topbar_shows_tenant_on_every_admin_page(app, admin_client):
    with app.app_context():
        tenant = db.session.get(Tenant, default_tenant_id(app))
        name, slug = tenant.name, tenant.slug
    html = admin_client.get('/admin/menu').get_data(as_text=True)
    topbar = html.split('id="adminTopbar"')[1].split('</header>')[0]
    assert name in topbar and '店家後台' not in topbar
    assert '點餐前台' in topbar and slug in topbar


@pytest.mark.e2e
def test_b309_e2e_void_needs_reason_and_restores_select(page, app):
    """看板選「已取消」先跳確認視窗：取消時選單還原、訂單不變；填原因送出後才作廢並記下原因"""
    from playwright.sync_api import expect

    from app.models.order import Order, OrderItem
    from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
    from tests.support import browser_staff_login
    tag = uuid.uuid4().hex[:6]
    name = f'AH2 作廢 {tag}'
    with app.app_context():
        order = Order(tenant_id=default_tenant_id(app), table_number='AH2', pickup_number=778, total_price=100,
                      payment_method='Cash', order_type='外帶', status='Pending', created_at=datetime.now())
        order.items = [OrderItem(tenant_id=order.tenant_id, item_name=name, price=100, quantity=1)]
        db.session.add(order)
        db.session.commit()
        order_id = order.id
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    page.goto('/admin?tab=orders')
    card = page.locator('#admin-orders-grid .card', has_text=name)
    select = card.locator('select[data-action="order-status"]')
    select.select_option('Cancelled')
    modal = page.locator('#adminVoidModal')
    expect(modal).to_be_visible()
    expect(modal).to_contain_text(f'單號 #{order_id}')
    modal.locator('button[data-bs-dismiss="modal"]').first.click()
    expect(modal).to_be_hidden()
    expect(page.locator('#admin-orders-grid .card', has_text=name).locator('select[data-action="order-status"]')).to_have_value('Pending')
    with app.app_context():
        assert db.session.get(Order, order_id).status == 'Pending', '取消確認不會作廢'

    page.locator('#admin-orders-grid .card', has_text=name).locator('select[data-action="order-status"]').select_option('Cancelled')
    expect(modal).to_be_visible()
    modal.locator('#adminVoidReason').fill('客人不要了')
    modal.locator('#adminVoidSubmit').click()
    expect(modal).to_be_hidden()
    with app.app_context():
        assert db.session.get(Order, order_id).status == 'Cancelled'


@pytest.mark.e2e
def test_b309_e2e_void_paid_order_with_manager_approval(page, app):
    """沒有退款權限、被授權報表的櫃檯：作廢已收款訂單時在同一個視窗輸入主管帳密核准 (不再卡在 403)"""
    from playwright.sync_api import expect

    from app.models.order import Order, OrderItem
    from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
    from tests.support import browser_staff_login
    name = f'AH2 核准 {uuid.uuid4().hex[:6]}'
    with app.app_context():
        order = Order(tenant_id=default_tenant_id(app), table_number='AH2', pickup_number=779, total_price=120,
                      payment_method='Cash', payment_status='paid', order_type='外帶', status='Pending',
                      created_at=datetime.now())
        order.items = [OrderItem(tenant_id=order.tenant_id, item_name=name, price=120, quantity=1)]
        db.session.add(order)
        db.session.commit()
        order_id = order.id
    with _staff(app, 'staff', 'reports') as client:
        browser_staff_login(page, client.staff_username, 'StaffPass!2026')
        page.goto('/admin?tab=orders')
        page.locator('#admin-orders-grid .card', has_text=name).locator('select[data-action="order-status"]').select_option('Cancelled')
        modal = page.locator('#adminVoidModal')
        expect(modal).to_be_visible()
        modal.locator('#adminVoidReason').fill('重複結帳')
        modal.locator('#adminVoidSubmit').click()
        expect(modal.locator('#adminVoidApproval')).to_be_visible()
        modal.locator('#adminVoidApproverUser').fill(ADMIN_USERNAME)
        modal.locator('#adminVoidApproverPass').fill(ADMIN_PASSWORD)
        modal.locator('#adminVoidSubmit').click()
        expect(modal).to_be_hidden()
    # 第一次送出預期收到 403 need_approval (瀏覽器會記一筆資源載入錯誤)，不算頁面錯誤
    page.problems[:] = [p for p in page.problems if 'status of 403' not in p]
    with app.app_context():
        assert db.session.get(Order, order_id).status == 'Cancelled'


def test_b309_live_board_has_void_confirmation(app, admin_client):
    html = admin_client.get('/admin').get_data(as_text=True)
    assert 'id="adminVoidModal"' in html and 'id="adminVoidReason"' in html and 'id="adminVoidApproverPass"' in html
    js = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'admin.js').read_text(encoding='utf-8')
    assert "select.value === 'Cancelled'" in js and 'need_approval' in js
    assert len(js.splitlines()) <= 500


def test_r2_016_hub_kitchen_topbar_has_no_back_to_kds(app, monkeypatch):
    """R2-016：線上端沒有廚房看板 (/kitchen 回 404)，廚房帳號的頂欄不顯示「回工作畫面」"""
    with _staff(app, 'kitchen', 'menu') as c:
        topbar = c.get('/admin').get_data(as_text=True).split('id="adminTopbar"')[1].split('</header>')[0]
        assert 'id="nav-back-work"' in topbar and '/kitchen' in topbar, '門市照常顯示'
        monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
        topbar = c.get('/admin').get_data(as_text=True).split('id="adminTopbar"')[1].split('</header>')[0]
        assert 'id="nav-back-work"' not in topbar
    with _staff(app, 'staff', 'menu') as c:
        topbar = c.get('/admin').get_data(as_text=True).split('id="adminTopbar"')[1].split('</header>')[0]
        assert 'id="nav-back-work"' in topbar, '線上端的櫃檯仍可回 POS'
