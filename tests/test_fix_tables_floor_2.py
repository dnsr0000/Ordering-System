"""
修正小組 tables-floor-2 的回歸測試：
- B-144 桌位排序超出範圍 / 門市網址過長時回提示訊息，不寫入資料庫 (PostgreSQL 上原本會 500)
- B-145 改名撞唯一索引回滾時，不再同時出現「已更新」的成功訊息
- R2-055 停用仍有未結帳訂單的桌位時提示警告
"""
from app.models.table import DiningTable
from app.models.tenant import Tenant
from app.services import table_service
from tests.support import checkout, flashes, line, make_item


def _create(admin_client, name):
    admin_client.post('/admin/tables', data={'action': 'create', 'names': name})
    flashes(admin_client)


def _table(app, name):
    with app.app_context():
        t = DiningTable.query.filter_by(name=name).first()
        return dict(id=t.id, token=t.token, sort_order=t.sort_order, is_active=t.is_active, name=t.name)


def test_sort_order_out_of_range_rejected(app, admin_client):
    _create(admin_client, 'FX2S1')
    t = _table(app, 'FX2S1')
    r = admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': t['id'], 'name': 'FX2S1',
                                                  'sort_order': '99999999999'})
    assert r.status_code == 302
    msgs = flashes(admin_client)
    assert any('排序請輸入' in m for m in msgs), msgs
    assert not any('已更新' in m for m in msgs), msgs
    assert _table(app, 'FX2S1')['sort_order'] == t['sort_order']

    admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': t['id'], 'name': 'FX2S1',
                                             'sort_order': '-5'})
    assert any('已更新' in m for m in flashes(admin_client))
    assert _table(app, 'FX2S1')['sort_order'] == -5


def test_base_url_too_long_rejected(app, admin_client):
    with app.app_context():
        before = [x.table_qr_base_url for x in Tenant.query.all()]
    long_url = 'http://192.168.1.10:5000/' + 'a' * 200
    r = admin_client.post('/admin/tables', data={'action': 'base_url', 'base_url': long_url})
    assert r.status_code == 302
    assert any('門市網址太長' in m for m in flashes(admin_client))
    with app.app_context():
        assert [x.table_qr_base_url for x in Tenant.query.all()] == before


def test_rename_conflict_does_not_flash_success(app, admin_client, monkeypatch):
    _create(admin_client, 'FX2R1, FX2R2')
    a = _table(app, 'FX2R1')

    def racy_rename(table, name):  # 模擬兩位店長同時改名：查重時尚無同名，commit 時才撞唯一索引
        table.name = str(name).strip()

    monkeypatch.setattr(table_service, 'rename_table', racy_rename)
    r = admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': a['id'], 'name': 'FX2R2',
                                                  'sort_order': a['sort_order']})
    assert r.status_code == 302
    msgs = flashes(admin_client)
    assert any('桌號剛被其他人使用' in m for m in msgs), msgs
    assert not any('已更新' in m for m in msgs), msgs
    assert _table(app, 'FX2R1')['name'] == 'FX2R1'


def test_deactivate_table_with_unpaid_orders_warns(app, admin_client):
    _create(admin_client, 'FX2U1')
    item = make_item(app, 'FX2 桌邊湯麵', price=120, stock=20)
    c = app.test_client()
    c.get(f"/t/{_table(app, 'FX2U1')['token']}")
    c.post('/guest_login')
    r = checkout(c, [line(item, 1)])
    assert r.status_code == 200, r.get_data(as_text=True)
    t = _table(app, 'FX2U1')

    admin_client.post('/admin/tables', data={'action': 'toggle', 'table_id': t['id']})
    with admin_client.session_transaction() as sess:
        flashed = sess.pop('_flashes', [])
    assert any(cat == 'warning' and '1 張未結帳訂單' in msg for cat, msg in flashed), flashed
    assert _table(app, 'FX2U1')['is_active'] is False

    admin_client.post('/admin/tables', data={'action': 'toggle', 'table_id': t['id']})  # 重新啟用：不需警告
    with admin_client.session_transaction() as sess:
        flashed = sess.pop('_flashes', [])
    assert any(cat == 'success' and '已啟用' in msg for cat, msg in flashed), flashed


def test_call_service_returns_translated_message(app, admin_client, store_settings):
    """B-486：桌邊呼叫服務的成功訊息另附目前顧客語言的 message_i18n"""
    store_settings('menu', languages=['en', 'ja'])
    _create(admin_client, 'FX2C1')
    c = app.test_client()
    c.get(f"/t/{_table(app, 'FX2C1')['token']}")
    with c.session_transaction() as sess:
        sess['lang'] = 'en'
    first = c.post('/api/table/call', json={'kind': 'bill'}).get_json()
    assert first['created'] is True
    assert first['message'] == '已通知服務人員：想結帳'
    assert first['message_i18n'] == 'Staff notified: Bill'
    again = c.post('/api/table/call', json={'kind': 'bill'}).get_json()
    assert again['created'] is False
    assert again['message_i18n'] == 'Staff already notified (Bill); please wait a moment'

    with c.session_transaction() as sess:
        sess['lang'] = 'zh'
    zh = c.post('/api/table/call', json={'kind': 'water'}).get_json()
    assert zh['message'] == zh['message_i18n'] == '已通知服務人員：加水 / 餐具'
