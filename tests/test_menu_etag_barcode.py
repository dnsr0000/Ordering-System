"""
點餐頁菜單狀態輪詢 /api/menu/availability (ETag / 304)、對外 API /api/v1/menu 的 ETag、
POS 條碼 (external_code) 與型別檢查設定。
"""
import os

from tests.support import login_staff, make_item
from tests.test_accounting_api_devices_archive import _create_key, no_rate_limit  # noqa: F401  (共用 fixture)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _kiosk(app):
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    return c


def test_menu_availability_etag_and_changes(app):
    from app.extensions import db
    from app.models.menu import MenuItem
    item = make_item(app, 'ETag 牛肉堡', price=120, stock=5)
    c = _kiosk(app)
    r = c.get('/api/menu/availability')
    assert r.status_code == 200 and r.mimetype == 'application/json'
    etag = r.headers['ETag']
    assert etag.startswith('"') and r.headers['Cache-Control'] == 'no-cache'
    body = r.get_json()
    assert body[str(item)] == {'available': True, 'hint': '', 'price': 120, 'original_price': 120,
                               'prices': {'dine_in': 120, 'takeout': 120, 'online': 120, 'delivery': 120},
                               # 沒有會員價時等於售價
                               'member_prices': {'dine_in': 120, 'takeout': 120, 'online': 120, 'delivery': 120}}
    # 沒變化：304 且無內容
    r = c.get('/api/menu/availability', headers={'If-None-Match': etag})
    assert r.status_code == 304 and r.data == b'' and r.headers['ETag'] == etag
    # 售完 → 內容與 ETag 都變
    with app.app_context():
        db.session.get(MenuItem, item).is_sold_out = True
        db.session.commit()
    r = c.get('/api/menu/availability', headers={'If-None-Match': etag})
    assert r.status_code == 200 and r.headers['ETag'] != etag
    assert r.get_json()[str(item)]['available'] is False
    # 不在供應時段：帶提示
    with app.app_context():
        it = db.session.get(MenuItem, item)
        it.is_sold_out, it.avail_days = False, ''.join(d for d in '1234567' if d != str(__import__('datetime').date.today().isoweekday()))
        db.session.commit()
    body = c.get('/api/menu/availability').get_json()
    assert body[str(item)]['available'] is False and '供應日' in body[str(item)]['hint']
    # 改價
    with app.app_context():
        it = db.session.get(MenuItem, item)
        it.avail_days, it.price = '1234567', 150
        db.session.commit()
    body = c.get('/api/menu/availability').get_json()
    assert body[str(item)] ['available'] is True and body[str(item)]['price'] == 150


def test_menu_availability_requires_no_login_but_tenant_scoped(app):
    from tests.support import other_shop_owner_client
    other_shop_owner_client(app, 'etag-other-shop')
    mine = make_item(app, 'ETag 本店餐', price=10)
    c = app.test_client()
    c.get('/?shop=etag-other-shop')
    body = c.get('/api/menu/availability').get_json()
    assert str(mine) not in body


def test_public_api_menu_etag(app, no_rate_limit):
    from tests.support import get_or_create_tenant
    from app.extensions import db
    from app.models.menu import MenuItem
    from tests.support import tenant_request
    tid = get_or_create_tenant(app, 'etag-api-shop')
    with tenant_request(app, tid):
        # 這家店要有品項，下面「每個品項都有 prep_minutes」的檢查才不會對空串列恆真
        if not MenuItem.query.filter_by(tenant_id=tid, name='ETag API 餐').first():
            db.session.add(MenuItem(tenant_id=tid, name='ETag API 餐', category='主餐', price=80, stock=10,
                                    total_stock=10, modifiers='none', prep_minutes=7))
            db.session.commit()
    owner = login_staff(app, 'etag_api_owner', 'owner', tenant_id=tid)
    key = _create_key(owner, ['menu:read'])
    c = app.test_client()
    h = {'Authorization': f'Bearer {key}'}
    r = c.get('/api/v1/menu', headers=h)
    assert r.status_code == 200 and r.get_json()['success'] is True
    etag = r.headers['ETag']
    assert c.get('/api/v1/menu', headers={**h, 'If-None-Match': etag}).status_code == 304
    assert c.get('/api/v1/menu', headers={**h, 'If-None-Match': '"nope"'}).status_code == 200
    items = r.get_json()['items']
    assert len(items) >= 1 and all('prep_minutes' in m for m in items)
    assert any(m['name'] == 'ETag API 餐' and m['prep_minutes'] == 7 for m in items)


def test_pos_menu_data_carries_external_code_for_scanner(app):
    item = make_item(app, '條碼可樂', price=35, external_code='4710088000012')
    pos = login_staff(app, 'barcode_staff', 'staff')
    html = pos.get('/pos').get_data(as_text=True)
    assert '4710088000012' in html and '掃條碼' in html
    with open(os.path.join(ROOT, 'static', 'js', 'pos', 'menu.js'), encoding='utf-8') as fh:
        js = fh.read()
    assert 'export function scanCode' in js and 'external_code' in js
    with open(os.path.join(ROOT, 'static', 'js', 'pos.js'), encoding='utf-8') as fh:
        assert 'scanBuffer' in fh.read()
    assert item


def _js_code(*parts):
    """讀 JS 檔並去掉註解 (只看實際程式碼，避免註解裡的字樣讓檢查恆真)"""
    import re
    with open(os.path.join(ROOT, 'static', 'js', *parts), encoding='utf-8') as fh:
        js = fh.read()
    js = re.sub(r'/\*.*?\*/', '', js, flags=re.S)
    return re.sub(r'(?m)(^|\s)//[^\n]*', r'\1', js)


def test_customer_live_module_wired():
    import re
    assert 'startAvailabilityPolling()' in _js_code('customer.js')
    live = _js_code('customer', 'live.js')
    # live.js 透過 Kiosk.pollJson 輪詢 (帶 ETag)，304 時 pollJson 回 null、不重畫
    assert "Kiosk.pollJson('/api/menu/availability', pollState)" in live
    assert 'if (payload) applyAvailability(payload)' in live
    # 實際送出 If-None-Match 並處理 304 的是 common.js 的 pollJson
    common = _js_code('common.js')
    m = re.search(r'async function pollJson\(url, state = \{\}\) \{(.*?)\n    \}', common, re.S)
    assert m, 'common.js 找不到 pollJson'
    body = m.group(1)
    assert "'If-None-Match': state.etag" in body and 'fetch(url, { headers' in body
    assert 'resp.status === 304) return null' in body and "state.etag = resp.headers.get('ETag')" in body
    assert len(re.findall(r'\bpollJson\b', common)) >= 2, 'pollJson 要公開在 Kiosk 物件上'


def test_mypy_config_present_and_passes_if_installed():
    import importlib.util
    import subprocess
    import sys
    with open(os.path.join(ROOT, 'pyproject.toml'), encoding='utf-8') as fh:
        assert '[tool.mypy]' in fh.read()
    # 以目前的直譯器判斷 / 執行 (Windows 用 .venv/Scripts/python 執行而未啟用 venv 時，PATH 找不到 mypy.exe)
    if importlib.util.find_spec('mypy') is None:
        import pytest
        pytest.skip('未安裝 mypy (pip install mypy)')
    proc = subprocess.run([sys.executable, '-m', 'mypy'], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
