"""端對端：每個頁面都能在瀏覽器中正常開啟 (沒有 JS 錯誤、沒有違反 CSP)"""
import pytest
from playwright.sync_api import expect

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import browser_member_login, browser_staff_login, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def demo_data(app):
    seed_demo(app)


@pytest.mark.parametrize('path', ['/?shop=default', '/register', '/face_login', '/counter', '/signage', '/tenant/register', '/admin',
                                  '/privacy?shop=default'])
def test_public_pages_load(page, path):
    resp = page.goto(path)
    assert resp.ok
    page.wait_for_load_state('networkidle')


@pytest.mark.parametrize('path', ['/admin', '/admin/staff', '/pos', '/kitchen', '/pickup', '/admin/availability', '/admin/staffing/auto', '/admin/system'])
def test_owner_pages_load(page, path):
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = page.goto(path)
    assert resp.ok
    page.wait_for_load_state('networkidle')


def test_member_pages_load(page):
    browser_member_login(page)
    for path in ('/', '/rewards'):
        assert page.goto(path).ok
        page.wait_for_load_state('networkidle')


def test_phone_width_layout(app, page):
    """手機寬度：看板頁 (取餐 / 叫號 / 廚房) 不可左右捲動；點餐頁頂欄換成多列時也不可蓋住下方內容"""
    page.set_viewport_size({'width': 390, 'height': 844})
    browser_staff_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
    for path in ('/pickup', '/kitchen', '/counter'):
        assert page.goto(path).ok
        page.wait_for_load_state('networkidle')
        overflow = page.evaluate('() => document.documentElement.scrollWidth - document.documentElement.clientWidth')
        assert overflow <= 1, f'{path} 在手機上左右溢出 {overflow}px'
    browser_member_login(page)   # 會員頂欄最長 (語言、大字、主題、我的訂單、購物車、登出)
    assert page.goto('/').ok
    page.wait_for_load_state('networkidle')
    covered = page.evaluate("""() => {
        const header = document.querySelector('header.header').getBoundingClientRect();
        const first = document.querySelector('main').firstElementChild.getBoundingClientRect();
        return first.top < header.bottom - 1;
    }""")
    assert not covered, '頂欄蓋住了下方內容'
    # 手機頂欄只有一列 (按鈕縮成圖示)；語言 / 主題等收在「⋯」裡，點開才出現
    header_h = page.evaluate("() => document.querySelector('header.header').getBoundingClientRect().height")
    assert header_h < 80, f'手機頂欄太高 ({header_h}px)'
    assert not page.locator('#themeToggle').is_visible()
    page.locator('.hdr-more-toggle').click()
    expect(page.locator('#themeToggle')).to_be_visible()
    expect(page.locator('#langSwitch')).to_be_visible()
    for sel in ('[data-action="my-orders"]', '[data-bs-target="#cart-sidebar"]', '[data-action="logout"]'):
        expect(page.locator(sel)).to_be_visible()
    # 桌機：全部按鈕帶文字排成一列，沒有「⋯」
    page.set_viewport_size({'width': 1280, 'height': 800})
    page.reload()
    page.wait_for_load_state('networkidle')
    assert page.evaluate("() => document.querySelector('header.header').getBoundingClientRect().height") < 80
    expect(page.locator('.hdr-more-toggle')).to_be_hidden()
    expect(page.locator('[data-action="my-orders"]')).to_contain_text('我的訂單')
