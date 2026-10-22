"""端對端：廚房看板 (KDS) 出餐、櫃檯取餐核銷、公開叫號看板隨 SSE 即時更新"""
import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.order import Order
from tests.support import (
    E2E_STAFF_PASSWORD,
    checkout,
    guest_client,
    line,
    login_staff,
    new_client_order_id,
    seed_demo,
)

pytestmark = pytest.mark.e2e

XSS_ITEM = "O'Brien 的「特製」<script>alert(1)</script>"
XSS_NOTE = '<img src=x>備註 "少鹽" \'快\''
XSS_CUSTOM = "<b>不要洋蔥</b>'辣'"


@pytest.fixture(autouse=True)
def demo(app):
    return seed_demo(app)


def staff_login(page, username):
    """以 API 登入員工 (不開啟後台頁面)，cookie 與瀏覽器分頁共用"""
    resp = page.request.post('/admin', form={'username': username, 'password': E2E_STAFF_PASSWORD})
    assert resp.ok, resp.status


def create_order(app, item_id, **fields):
    """以訪客身分結帳一筆外帶訂單，回傳 (訂單編號, 取餐號)"""
    payload = dict(order_type='外帶', note=XSS_NOTE, need_cutlery=True, client_order_id=new_client_order_id(), **fields)
    r = checkout(guest_client(app), [line(item_id, 2, customization=XSS_CUSTOM)], **payload)
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    return data['order_id'], data['pickup_number']


def order_status(app, order_id):
    with app.app_context():
        return db.session.get(Order, order_id).status


def wait_for_status(page, app, order_id, status):
    """API 回應後資料庫才更新；樂觀更新的畫面會先變，因此輪詢等待資料庫結果"""
    for _ in range(50):
        if order_status(app, order_id) == status:
            return
        page.wait_for_timeout(100)
    assert order_status(app, order_id) == status


def set_status(app, order_id, status, username='e2e_kds_helper', role='kitchen'):
    client = login_staff(app, username, role)
    r = client.post(f'/api/orders/{order_id}/status', json={'status': status})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_kitchen_display_completes_order(page, app, demo):
    order_id, pickup = create_order(app, demo[XSS_ITEM])
    staff_login(page, 'e2e_kitchen')
    page.goto('/kitchen')

    card = page.locator(f'#ordersGrid [data-order-id="{order_id}"]')
    expect(card).to_be_visible()
    expect(card.locator('.kds-pickup-number')).to_have_text(f'#{pickup:03d}')
    # 使用者資料一律以純文字顯示
    expect(card.get_by_text(XSS_ITEM, exact=True)).to_be_visible()
    expect(card.get_by_text(XSS_CUSTOM)).to_be_visible()
    expect(card.get_by_text(f'備註：{XSS_NOTE}')).to_be_visible()
    expect(card.get_by_text('x 2')).to_be_visible()

    # 新訂單透過 SSE 即時出現 (不重新整理頁面)
    second_id, _ = create_order(app, demo['示範薯條'])
    second = page.locator(f'#ordersGrid [data-order-id="{second_id}"]')
    expect(second).to_be_visible()

    # 完成出餐：製作中 → 已出餐
    assert order_status(app, order_id) == 'Pending'
    card.get_by_role('button', name='完成出餐').click()
    expect(card).to_have_count(0)
    wait_for_status(page, app, order_id, 'Completed')
    expect(second).to_be_visible()

    # 其他人 (例如 POS) 完成出餐時，看板也會即時移除
    set_status(app, second_id, 'Completed')
    expect(second).to_have_count(0)
    badge = int(page.locator('#pendingCountBadge').inner_text())
    assert badge == page.locator('#ordersGrid [data-order-id]').count()


def test_counter_board_updates_via_sse(page, app, demo):
    order_id, pickup = create_order(app, demo['示範紅茶'])
    page.goto('/counter')

    preparing = page.locator(f'#preparingList [data-order-id="{order_id}"]')
    ready = page.locator(f'#readyOrdersGrid [data-order-id="{order_id}"]')
    expect(preparing).to_be_visible()
    expect(preparing).to_contain_text(f'取餐 #{pickup:03d}')
    expect(ready).to_have_count(0)

    # 廚房出餐 → 叫號看板即時顯示取餐號碼
    set_status(app, order_id, 'Completed')
    expect(ready).to_be_visible()
    expect(ready.locator('.order-number-display')).to_have_text(f'#{pickup:03d}')
    expect(preparing).to_have_count(0)

    # 櫃檯核銷取餐 → 從叫號看板移除
    set_status(app, order_id, 'PickedUp', username='e2e_counter_helper', role='staff')
    expect(ready).to_have_count(0)


def test_pickup_page_marks_orders_picked_up(page, app, demo):
    first_id, _ = create_order(app, demo[XSS_ITEM])
    second_id, second_pickup = create_order(app, demo['示範薯條'])
    staff_login(page, 'e2e_pos')
    page.goto('/pickup')

    first = page.locator(f'#pickupCardsGrid [data-order-id="{first_id}"]')
    second = page.locator(f'#pickupCardsGrid [data-order-id="{second_id}"]')
    expect(first).to_have_count(0)  # 尚未出餐的訂單不會出現在取餐核銷

    set_status(app, first_id, 'Completed')
    set_status(app, second_id, 'Completed')
    expect(first).to_be_visible()  # SSE 通知後自動出現
    expect(second).to_be_visible()
    expect(first.get_by_text(XSS_ITEM, exact=True)).to_be_visible()
    expect(first.get_by_text(XSS_CUSTOM)).to_be_visible()

    # 點按鈕核銷
    first.get_by_role('button', name='顧客已取餐').click()
    expect(first).to_have_count(0)
    wait_for_status(page, app, first_id, 'PickedUp')

    # 輸入取餐號碼快速核銷
    page.fill('#quickSearchInput', str(second_pickup))
    page.press('#quickSearchInput', 'Enter')
    expect(second).to_have_count(0)
    wait_for_status(page, app, second_id, 'PickedUp')
    assert page.locator('#readyCountBadge').inner_text() == str(page.locator('#pickupCardsGrid [data-order-id]').count())


def test_kitchen_and_pickup_have_no_inline_handlers(page, app):
    """KDS / 取餐頁的動態內容只用 data-action，不會產生 onclick 等行內事件"""
    create_order(app, seed_demo(app)['示範薯條'])
    staff_login(page, 'e2e_manager')
    for path, grid in (('/kitchen', '#ordersGrid'), ('/pickup', '#pickupCardsGrid')):
        page.goto(path)
        page.wait_for_load_state('networkidle')
        assert page.locator(f'{grid} [onclick]').count() == 0
        assert page.locator('script:not([src]):not([type="application/json"])').count() == 0
