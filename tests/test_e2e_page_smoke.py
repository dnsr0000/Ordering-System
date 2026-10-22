"""端對端：每個頁面都能在瀏覽器中正常開啟 (沒有 JS 錯誤、沒有違反 CSP)"""
import pytest

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import browser_member_login, browser_staff_login, seed_demo

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def demo_data(app):
    seed_demo(app)


@pytest.mark.parametrize('path', ['/?shop=default', '/register', '/face_login', '/counter', '/tenant/register', '/admin',
                                  '/privacy?shop=default'])
def test_public_pages_load(page, path):
    resp = page.goto(path)
    assert resp.ok
    page.wait_for_load_state('networkidle')


@pytest.mark.parametrize('path', ['/admin', '/admin/staff', '/pos', '/kitchen', '/pickup'])
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
