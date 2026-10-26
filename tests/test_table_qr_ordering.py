"""
門市桌邊掃碼點餐：後台桌位管理 (新增範圍、改名、停用、重新產生 QR code、刪除、門市網址)、列印桌卡、
顧客掃碼綁定桌位後點餐 (內用、標記桌號、未結帳)、POS 桌位結帳、失效 QR code 的處理。
"""
import re

from app.extensions import db
from app.models.order import Order
from app.models.table import DiningTable
from app.models.user import StaffAuditLog
from tests.support import assert_flash, checkout, line, login_staff, make_item, make_member, member_client


def _table(app, name):
    with app.app_context():
        t = DiningTable.query.filter_by(name=name).first()
        return None if t is None else dict(id=t.id, token=t.token, is_active=t.is_active)


def _scan(app, token, client=None):
    c = client or app.test_client()
    r = c.get(f'/t/{token}')
    return c, r


def _order(app, order_id):
    with app.app_context():
        o = db.session.get(Order, order_id)
        return dict(source=o.order_source, table=o.table_number, payment=o.payment_status, order_type=o.order_type,
                    dining_table_id=o.dining_table_id, method=o.payment_method)


def test_manager_creates_tables_with_ranges(app, admin_client):
    r = admin_client.post('/admin/tables', data={'action': 'create', 'names': 'T1-T3, 吧台1'})
    assert r.status_code == 302
    assert_flash(admin_client, '已新增 4 桌')
    admin_client.post('/admin/tables', data={'action': 'create', 'names': 'T2-T4'})
    assert_flash(admin_client, '已存在略過：T2, T3')
    with app.app_context():
        names = [t.name for t in DiningTable.query.order_by(DiningTable.sort_order).all()
                 if t.name in ('T1', 'T2', 'T3', '吧台1', 'T4')]
    assert names == ['T1', 'T2', 'T3', '吧台1', 'T4']
    admin_client.post('/admin/tables', data={'action': 'create', 'names': '<script>'})
    assert_flash(admin_client, '只能使用')
    page = admin_client.get('/admin/tables').get_data(as_text=True)
    assert 'T1' in page and '/t/' in page


def test_only_managers_manage_tables(app, staff_client, kitchen_client):
    assert staff_client.get('/admin/tables').status_code == 403
    assert kitchen_client.post('/admin/tables', data={'action': 'create', 'names': 'X1'}).status_code == 403
    assert app.test_client().get('/admin/tables').status_code == 302  # 未登入導向登入頁


def test_print_cards_use_configured_store_url(app, admin_client):
    admin_client.post('/admin/tables', data={'action': 'base_url', 'base_url': 'http://192.168.50.10:5000'})
    html = admin_client.get('/admin/tables/print').get_data(as_text=True)
    token = _table(app, 'T1')['token']
    assert f'http://192.168.50.10:5000/t/{token}' in html
    assert html.count('data:image/svg+xml;base64,') >= 4, '每桌一張 QR code 圖片'
    admin_client.post('/admin/tables', data={'action': 'base_url', 'base_url': 'javascript:alert(1)'})
    assert_flash(admin_client, '門市網址格式不正確')


def test_scan_order_and_settle_at_pos(app, admin_client, staff_client):
    item = make_item(app, '桌邊牛肉麵', price=180, stock=20)
    c, r = _scan(app, _table(app, 'T1')['token'])
    assert r.status_code == 302 and r.headers['Location'].endswith('/')
    landing = c.get('/').get_data(as_text=True)
    assert 'table-banner' in landing and 'T1' in landing, '未登入時也顯示桌號'
    c.post('/guest_login')
    page = c.get('/').get_data(as_text=True)
    assert 'id="table-checkout-hint"' in page and 'id="orderType" value="內用"' in page
    assert '"table": "T1"' in page, '頁面資料告知前端為桌邊點餐 (不自動登出)'

    r = checkout(c, [line(item, 2)], order_type='外帶', payment_method='Line Pay')
    assert r.status_code == 200, r.get_data(as_text=True)
    first = r.get_json()['order_id']
    o = _order(app, first)
    assert o == dict(source='Table', table='T1 桌', payment='unpaid', order_type='內用',
                     dining_table_id=_table(app, 'T1')['id'], method='Cash'), '固定內用、餐後結帳'
    second = checkout(c, [line(item, 1)]).get_json()['order_id']

    # 公開叫號看板顯示桌號 (不是遮罩後的字)
    board = app.test_client().get('/api/counter_orders').get_json()
    assert 'T1 桌' in [x['user_name'] for x in board['preparing_orders']]

    tables = staff_client.get('/api/pos/today_orders').get_json()['unpaid_tables']
    t1 = next(t for t in tables if t['name'] == 'T1')
    assert t1['total'] == 180 * 3 and [o['id'] for o in t1['orders']] == [first, second]

    tid = _table(app, 'T1')['id']
    short = staff_client.post(f'/api/pos/tables/{tid}/settle', json={'payment_method': 'Cash', 'cash_received': 100})
    assert short.status_code == 400 and '小於應付' in short.get_json()['message']
    ok = staff_client.post(f'/api/pos/tables/{tid}/settle', json={'payment_method': 'Cash', 'cash_received': 600})
    assert ok.status_code == 200 and (ok.get_json()['total'], ok.get_json()['change']) == (540, 60)
    assert _order(app, first)['payment'] == 'settled' and _order(app, second)['payment'] == 'settled'
    assert staff_client.post(f'/api/pos/tables/{tid}/settle', json={'payment_method': 'Cash'}).status_code == 409
    with app.app_context():
        audit = StaffAuditLog.query.filter_by(action_type='TABLE_SETTLE').order_by(StaffAuditLog.id.desc()).first()
        assert 'T1 桌結帳' in audit.details and '找零:$60' in audit.details
    assert not [t for t in staff_client.get('/api/pos/today_orders').get_json()['unpaid_tables'] if t['name'] == 'T1']


def test_member_can_order_from_table(app):
    item = make_item(app, '桌邊會員餐', price=100, stock=20)
    make_member(app, '桌邊會員', '0933880001', pin='2580')
    c = member_client(app, '0933880001', '2580')
    _scan(app, _table(app, 'T2')['token'], client=c)
    order_id = checkout(c, [line(item)]).get_json()['order_id']
    o = _order(app, order_id)
    assert o['table'] == 'T2 桌 (桌邊會員)' and o['payment'] == 'unpaid'


def test_kitchen_cannot_settle(app, kitchen_client):
    tid = _table(app, 'T2')['id']
    assert kitchen_client.post(f'/api/pos/tables/{tid}/settle', json={}).status_code == 403


def test_disabled_rotated_or_unknown_qr_cannot_order(app, admin_client):
    item = make_item(app, '失效測試餐', price=50, stock=20)
    t3 = _table(app, 'T3')
    c, _ = _scan(app, t3['token'])
    c.post('/guest_login')
    admin_client.post('/admin/tables', data={'action': 'rotate', 'table_id': t3['id']})
    assert _scan(app, t3['token'])[1].status_code == 404, '舊的 QR code 失效'
    r = checkout(c, [line(item)])
    assert r.status_code == 409 and '重新掃描' in r.get_json()['message'], '已綁定的手機也失效 (不可變成一般點餐機訂單)'
    assert checkout(c, [line(item)]).status_code == 409, '再送一次也一樣被拒 (失效後不會變回一般點餐機)'
    assert 'table-expired-banner' in c.get('/').get_data(as_text=True), '頁面提示重新掃碼'
    c.post('/logout')
    c.post('/guest_login')
    assert checkout(c, [line(item)]).status_code == 409, '登出再以訪客登入仍需重新掃碼'

    new_token = _table(app, 'T3')['token']
    _scan(app, new_token, client=c)
    admin_client.post('/admin/tables', data={'action': 'toggle', 'table_id': t3['id']})
    assert _scan(app, new_token)[1].status_code == 404, '停用的桌位不可掃碼'
    assert checkout(c, [line(item)]).status_code == 409
    assert app.test_client().get('/t/not-a-real-token').status_code == 404


def test_table_with_unpaid_orders_cannot_be_deleted(app, admin_client, staff_client):
    item = make_item(app, '刪桌測試餐', price=60, stock=20)
    t4 = _table(app, 'T4')
    c, _ = _scan(app, t4['token'])
    c.post('/guest_login')
    assert checkout(c, [line(item)]).status_code == 200
    admin_client.post('/admin/tables', data={'action': 'delete', 'table_id': t4['id']})
    assert_flash(admin_client, '未結帳')
    assert _table(app, 'T4') is not None
    staff_client.post(f"/api/pos/tables/{t4['id']}/settle", json={'payment_method': 'Credit Card', 'auth_code': 'A12345'})
    admin_client.post('/admin/tables', data={'action': 'delete', 'table_id': t4['id']})
    assert _table(app, 'T4') is None


def test_rename_and_cross_tenant(app, admin_client):
    t = _table(app, '吧台1')
    admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': t['id'], 'name': 'T1', 'sort_order': 9})
    assert_flash(admin_client, '已存在')
    admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': t['id'], 'name': '吧台A', 'sort_order': 9})
    assert _table(app, '吧台A') is not None
    from tests.support import other_shop_owner_client
    other = other_shop_owner_client(app)
    assert other.post('/admin/tables', data={'action': 'toggle', 'table_id': t['id']}).status_code == 404
    assert re.search(r'吧台A', admin_client.get('/admin/tables').get_data(as_text=True))


def test_not_available_on_hub(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    assert admin_client.get('/admin/tables').status_code == 404
    assert app.test_client().get(f"/t/{_table(app, 'T1')['token']}").status_code == 404
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'standalone')
    assert login_staff(app, 'table_staff_x', 'staff').get('/pos').status_code == 200
