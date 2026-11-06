"""端對端：客服對話窗 (選項換行、你是否要問) → 轉接真人客服 → 店員在後台「客服對話」回覆 → 顧客即時看到"""
import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models.order import Order
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import browser_staff_login, checkout, guest_client, line, new_client_order_id, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def demo(app):
    return seed_demo(app)


def _order_uuid(app, item_id):
    r = checkout(guest_client(app), [line(item_id, 1)], order_type='外帶', client_order_id=new_client_order_id())
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        return db.session.get(Order, r.get_json()['order_id']).uuid


def test_chat_handoff_to_staff_and_reply(page, app, demo, store_settings, browser, live_url):
    store_settings('chatbot', enabled=True, llm_enabled=False, handoff_enabled=True, fallback='這個我不清楚',
                   faq='停車 車位 => 門口有機車位')
    page.set_viewport_size({'width': 400, 'height': 760})   # 手機寬度：建議問題要換行，不可擠成一列橫向捲動
    assert page.goto(f'/track/{_order_uuid(app, demo["示範紅茶"])}').ok
    page.locator('.chatbot-fab').click()
    suggest = page.locator('#chatbotSuggest')
    expect(suggest.locator('.chatbot-chip').first).to_be_visible()
    assert suggest.evaluate('el => el.scrollWidth <= el.clientWidth + 1'), '建議問題沒有橫向捲動'

    page.fill('#chatbotInput', '講個笑話給我聽')
    page.press('#chatbotInput', 'Enter')
    answer = page.locator('.chatbot-bot').last
    expect(answer).to_contain_text('這個我不清楚')
    expect(answer.locator('.chatbot-dym-title')).to_have_text('你是否要問：')
    expect(answer.locator('.chatbot-dym .chatbot-chip')).to_have_count(3)

    answer.locator('[data-action="chat-handoff"]').click()
    expect(page.locator('#chatbotName')).to_have_text('真人客服對話中')
    expect(page.locator('.chatbot-notice').last).to_contain_text('已為您轉接真人客服')
    page.fill('#chatbotInput', '請問有兒童椅嗎')
    page.press('#chatbotInput', 'Enter')
    expect(page.locator('.chatbot-user').last).to_have_text('請問有兒童椅嗎')

    # 店員在另一個瀏覽器登入後台回覆
    staff_ctx = browser.new_context(base_url=live_url, viewport={'width': 1366, 'height': 900}, locale='zh-TW')
    staff = staff_ctx.new_page()
    staff.on('dialog', lambda d: d.accept())
    try:
        browser_staff_login(staff, ADMIN_USERNAME, ADMIN_PASSWORD)
        assert staff.goto('/admin/support').ok
        item = staff.locator('#supportList .support-item', has_text='取餐').first
        expect(item).to_contain_text('待回覆')
        item.click()
        expect(staff.locator('#supportMessages')).to_contain_text('請問有兒童椅嗎')
        expect(staff.locator('#supportMessages')).to_contain_text('講個笑話給我聽')   # 附上與 AI 的對話
        staff.fill('#supportInput', '有的，入座時跟店員說一聲即可')
        staff.press('#supportInput', 'Enter')
        expect(staff.locator('#supportMessages .support-staff').last).to_contain_text('有的，入座時跟店員說一聲即可')

        staff_reply = page.locator('.chatbot-staff').last
        expect(staff_reply).to_contain_text('有的，入座時跟店員說一聲即可', timeout=10000)
        expect(staff_reply.locator('.chatbot-who')).to_have_text('客服人員')

        staff.locator('#supportCloseBtn').click()
        expect(staff.locator('#supportStatus')).to_have_text('已結束')
    finally:
        staff_ctx.close()
    expect(page.locator('.chatbot-notice').last).to_contain_text('真人客服已結束', timeout=10000)
    expect(page.locator('#chatbotName')).not_to_have_text('真人客服對話中')
