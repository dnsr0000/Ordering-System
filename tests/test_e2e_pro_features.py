"""端對端：進階功能頁面都能在瀏覽器正常開啟 (沒有 JS 錯誤 / CSP 違規)，以及 POS 的交班、整單折扣 (主管核准)、外送單、
點餐頁多語系與電子發票欄位等實際操作"""
import re

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.order import Order
from app.models.shift import CashShift
from app.services import settings_service
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import browser_staff_login, checkout, line, login_staff, seed_demo

pytestmark = pytest.mark.e2e

ADMIN_PAGES = ['/admin/settings', '/admin/invoices', '/admin/refunds', '/admin/shifts', '/admin/printers',
               '/admin/menu_pro', '/admin/inventory', '/admin/members_pro', '/admin/promotions', '/admin/delivery',
               '/admin/reports', '/admin/timeclock', '/admin/system', '/reservations', '/admin/purchasing', '/admin/forecast',
               '/admin/live', '/admin/gift_cards', '/admin/feedback', '/admin/security', '/admin/dispatch']


@pytest.fixture(autouse=True)
def demo_data(app):
    return seed_demo(app)


@pytest.fixture
def close_shifts(app):
    def run():
        with app.app_context():
            CashShift.query.filter_by(status='open').update({'status': 'closed'})
            db.session.commit()
    run()
    yield
    run()


@pytest.mark.parametrize('path', ADMIN_PAGES)
def test_pro_admin_pages_load(page, path):
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = page.goto(path)
    assert resp.ok, path
    page.wait_for_load_state('networkidle')


def test_every_settings_section_renders(page):
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    for key in settings_service.SECTIONS:
        assert page.goto(f'/admin/settings/{key}').ok, key
        page.wait_for_load_state('networkidle')
    # 金鑰欄位不回填到頁面
    assert page.goto('/admin/settings/linepay').ok
    expect(page.locator('input[type="password"]').first).to_have_value('')


def test_public_pro_pages_load(app, page, store_settings):
    store_settings('reservation', online_enabled=True)
    with app.app_context():
        from tests.support import guest_client
    c = guest_client(app)
    from tests.support import make_item
    iid = make_item(app, 'E2E 追蹤餐', price=30, stock=5)
    uuid = checkout(c, [line(iid)]).get_json()['order_uuid']
    for path in ('/reserve?shop=default', '/waitlist?shop=default', '/timeclock?shop=default', '/offline',
                 f'/track/{uuid}'):
        assert page.goto(path).ok, path
        page.wait_for_load_state('networkidle')
    page.goto('/waitlist?shop=default')
    page.fill('input[name="name"]', '瀏覽器候位')
    page.fill('input[name="party_size"]', '3')
    page.locator('form button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    assert '/waitlist/' in page.url


def test_kitchen_station_tabs_and_counter(page, app, admin_client):
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': 'E2E 飲料站', 'categories': '飲料'})
    from app.models.printing import KitchenStation
    with app.app_context():
        sid = KitchenStation.query.filter_by(name='E2E 飲料站').first().id
    try:
        browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
        assert page.goto(f'/kitchen?station={sid}').ok
        page.wait_for_load_state('networkidle')
        expect(page.locator('body')).to_contain_text('E2E 飲料站')
        assert page.goto('/counter').ok
        page.wait_for_load_state('networkidle')
    finally:
        with app.app_context():
            KitchenStation.query.filter_by(id=sid).delete()
            db.session.commit()


def test_pos_shift_discount_approval_and_card(app, page, close_shifts):
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')

    # 開班 → 零用金支出 → 顯示應有現金
    page.locator('#posShiftBtn').click()
    expect(page.locator('#posShiftModal')).to_be_visible()
    page.fill('#posShiftOpening', '1500')
    page.locator('#posShiftOpenForm button[type="submit"]').click()
    expect(page.locator('#posShiftOpen')).to_be_visible()
    page.select_option('#posShiftMoveKind', 'pay_out')
    page.fill('#posShiftMoveAmount', '100')
    page.fill('#posShiftMoveNote', '買冰塊')
    page.locator('#posShiftMoveForm button[type="submit"]').click()
    expect(page.locator('#posShiftSummary')).to_contain_text('1,400')
    page.keyboard.press('Escape')
    expect(page.locator('#posShiftModal')).to_be_hidden()
    expect(page.locator('#posShiftBadge')).to_have_text('開班中')

    # 整單折扣 10% (員工沒有折扣權限 → 結帳時跳出主管核准)
    page.locator('#posMenuGrid').get_by_text('示範雞塊', exact=True).click()
    page.locator('#posMenuGrid').get_by_text('示範雞塊', exact=True).click()
    page.locator('[data-action="openOrderDiscount"]').click()
    expect(page.locator('#posDiscountModal')).to_be_visible()
    page.locator('[data-action="discountPreset"][data-type="percent"][data-value="10"]').click()
    page.locator('#posDiscountForm button[type="submit"]').click()
    expect(page.locator('#posManualRow')).to_be_visible()
    expect(page.locator('#posManualDiscount')).to_have_text('14')
    expect(page.locator('#posFinalTotal')).to_have_text('126')
    page.select_option('#posPaymentMethod', 'Credit Card')
    page.fill('#posCardAuth', 'E2E777')
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posApprovalModal')).to_be_visible()
    page.fill('#posApproverUser', ADMIN_USERNAME)
    page.fill('#posApproverPass', 'wrong-password')
    page.locator('#posApprovalForm button[type="submit"]').click()
    expect(page.locator('#posApprovalMsg')).to_contain_text('密碼錯誤')   # 視窗保持開啟並顯示錯誤
    expect(page.locator('#posApprovalModal')).to_be_visible()
    page.fill('#posApproverPass', ADMIN_PASSWORD)
    page.locator('#posApprovalForm button[type="submit"]').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    with app.app_context():
        o = db.session.get(Order, order_id)
        assert (o.total_price, o.manual_discount, o.approved_by, o.payment_reference) == (126, 14, ADMIN_USERNAME, 'E2E777')
        assert o.shift_id is not None
    page.locator('#rcpt-close-btn').click()

    # 關班
    page.locator('#posShiftBtn').click()
    expect(page.locator('#posShiftOpen')).to_be_visible()
    page.locator('#posShiftDenoms input').first.fill('1')
    page.uncheck('#posShiftPrint')
    page.locator('#posShiftCloseForm button[type="submit"]').click()
    expect(page.locator('#posShiftResult')).to_be_visible()
    with app.app_context():
        assert CashShift.query.filter_by(status='open').count() == 0
    # 需要核准時伺服器回應 403 (need_approval)，瀏覽器會把它記成資源載入錯誤：屬預期行為
    page.problems[:] = [p for p in page.problems if 'status of 403' not in p]


def test_pos_delivery_mode(app, page):
    browser_staff_login(page, 'e2e_pos')
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('#posDeliveryBtn').click()
    expect(page.locator('#posDeliveryPanel')).to_be_visible()
    page.locator('#posDeliveryBtn').click()
    expect(page.locator('#posDeliveryPanel')).to_be_hidden()


def test_pos_live_orders_refund_modal(app, page):
    login_staff(app, 'e2e_pos')
    from tests.support import make_item
    iid = make_item(app, 'E2E 退款餐', price=90, stock=5)
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert page.goto('/pos').ok
    page.wait_for_load_state('networkidle')
    page.locator('#posMenuGrid').get_by_text('E2E 退款餐', exact=True).click()
    page.fill('#posCashReceived', '100')
    page.locator('#posSubmitBtn').click()
    expect(page.locator('#posReceiptModal')).to_be_visible()
    order_id = int(page.locator('#rcpt-order-id').inner_text().lstrip('#'))
    page.locator('#rcpt-close-btn').click()
    page.locator('[data-action="openLiveOrders"]').first.click()
    expect(page.locator('#posLiveOrdersModal')).to_be_visible()
    row_btn = page.locator(f'[data-action="openRefund"][data-id="{order_id}"]')
    expect(row_btn).to_be_visible()
    row_btn.click()
    expect(page.locator('#posRefundModal')).to_be_visible()


def test_customer_page_languages_and_invoice(app, page, store_settings):
    store_settings('menu', languages=['en', 'ja'])
    store_settings('einvoice', enabled=True)
    assert page.goto('/?shop=default').ok
    page.get_by_role('button', name=re.compile('訪客直接點餐')).click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#cart-count')).to_be_visible()
    assert page.goto('/?lang=en').ok
    page.wait_for_load_state('networkidle')
    expect(page.locator('#langSwitch')).to_be_visible()
    expect(page.locator('body')).to_contain_text('Add to cart')
    assert page.goto('/?lang=zh').ok
    page.wait_for_load_state('networkidle')


def test_admin_issue_coupon_and_split_ticket_view(app, page, admin_client):
    """後台：建立會員專屬券 → 按「發放」以手機清單發券 → 會員的票券視窗分成未使用 / 已使用 (近 7 天)"""
    from datetime import datetime, timedelta

    from app.models.coupon import Coupon, UserCoupon
    from tests.support import make_member
    make_member(app, 'E2E 發券會員', '0968800001')
    admin_client.post('/admin/add_coupon', data={'title': 'E2E 專屬券', 'code': 'E2EGIFT', 'discount_type': 'fixed',
                                                 'discount_value': '20', 'reward_points': '0', 'member_only': '1',
                                                 'valid_days': '30'})
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert page.goto('/admin?tab=rewards').ok
    page.wait_for_load_state('networkidle')
    with app.app_context():
        cid = Coupon.query.filter_by(code='E2EGIFT').first().id
    page.locator(f'[data-action="issue-coupon"][data-id="{cid}"]').click()
    expect(page.locator('#issueCouponModal')).to_be_visible()
    expect(page.locator('#issue-coupon-period')).to_contain_text('領取後 30 天')
    page.select_option('#issue-target', 'phones')
    expect(page.locator('#issue-phones-row')).to_be_visible()
    page.fill('#issue-phones', '0968800001')
    page.fill('#issue-quantity', '2')
    page.locator('#issueCouponForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('body')).to_contain_text('1 位會員，共 2 張')
    expect(page.locator('#couponBatchTable')).to_contain_text('E2E 專屬券')

    with app.app_context():
        ucs = UserCoupon.query.filter_by(code='E2EGIFT').order_by(UserCoupon.id).all()
        ucs[0].is_used, ucs[0].used_at = True, datetime.now() - timedelta(days=1)
        db.session.commit()
    assert page.goto('/admin?tab=users').ok
    page.wait_for_load_state('networkidle')
    page.locator('tr', has_text='0968800001').locator('[data-action="user-coupons"]').click()
    body = page.locator('#userCouponsModalBody')
    expect(body).to_contain_text('未使用 (1)')
    expect(body).to_contain_text('已使用 / 已過期 (近 7 天，1)')
    body.locator('[data-action="toggle-user-coupon"]').first.click()
    expect(body).to_contain_text('未使用 (0)')


def test_rewards_remaining_badge_and_coupon_limits_form(app, page, admin_client):
    """紅利商城：已持有的券顯示「剩餘 N 張」，兌換視窗提示目前張數；後台編輯視窗帶出防濫用設定"""
    from app.models.coupon import Coupon
    from tests.support import browser_member_login, make_member
    make_member(app, 'E2E 剩餘張數', '0968800009', points=300, pin='1357')
    admin_client.post('/admin/add_coupon', data={'title': 'E2E 可重複換券', 'code': 'E2EREPEAT', 'discount_type': 'percent',
                                                 'discount_value': '90', 'reward_points': '5', 'max_discount': '80',
                                                 'uses_per_member': '0', 'valid_days': '14'})
    with app.app_context():
        cid = Coupon.query.filter_by(code='E2EREPEAT').first().id
    browser_member_login(page, phone='0968800009', pin='1357')
    assert page.goto('/rewards').ok
    page.wait_for_load_state('networkidle')
    btn = page.locator(f'[data-action="redeem-coupon"][data-id="{cid}"]')
    expect(btn).to_have_text('立即兌換')
    btn.click()
    page.locator('#confirmRedeemBtn').click()
    page.wait_for_load_state('networkidle')
    card = page.locator('.reward-card', has=page.locator(f'[data-action="redeem-coupon"][data-id="{cid}"]'))
    expect(card.locator('.held-badge')).to_have_text('剩餘 1 張')
    expect(card).to_contain_text('最高折 $80')
    expect(btn).to_have_text('再兌換一張')
    btn.click()
    expect(page.locator('body')).to_contain_text('您目前還有 1 張未使用')

    page2 = page.context.new_page()
    browser_staff_login(page2, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert page2.goto('/admin?tab=rewards').ok
    page2.wait_for_load_state('networkidle')
    page2.locator(f'[data-action="edit-coupon"][data-id="{cid}"]').click()
    expect(page2.locator('#edit-coupon-max-discount')).to_have_value('80')
    expect(page2.locator('#edit-coupon-code')).to_have_value('E2EREPEAT')


def test_security_page_2fa_setup_and_member_search(app, page):
    """帳號安全頁：設定驗證 App (QR code、確認啟用、備用碼)；會員名冊搜尋"""
    from app.services import security_service
    from tests.support import make_member
    login_staff(app, 'e2e_tfa_mgr', 'manager')
    make_member(app, 'E2E 搜尋會員', '0968800077')
    browser_staff_login(page, 'e2e_tfa_mgr')
    assert page.goto('/admin/security').ok
    page.wait_for_load_state('networkidle')
    page.locator('#start2faBtn').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('img.totp-qr')).to_be_visible()
    secret = page.locator('#totpSecret').inner_text().strip()
    page.fill('#confirmTotpCode', security_service.totp_now(secret))
    page.locator('#confirmTotpForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#backupCodes')).to_be_visible()
    expect(page.locator('#sessionTable')).to_contain_text('這台')

    assert page.goto('/admin?tab=users').ok
    page.wait_for_load_state('networkidle')
    page.fill('#memberSearchForm input[name="uq"]', '0968800077')
    page.locator('#memberSearchForm button[type="submit"]').click()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#memberTable tbody tr')).to_have_count(1)
    expect(page.locator('#memberTable')).to_contain_text('E2E 搜尋會員')
    page.locator('#memberTable [data-action="user-coupons"]').click()
    expect(page.locator('#userCouponsModalBody')).to_contain_text('未使用 (0)')
