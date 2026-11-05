"""數位看板 (/signage，免登入)：菜單與售完標示、分類順序、促銷標語、取餐叫號、ETag 與後台設定"""
from datetime import date

from tests.support import default_tenant_id, make_item


def _board(app):
    c = app.test_client()
    c.get('/?shop=default')
    return c


def _items_by_name(data):
    return {i['name']: i for cat in data['categories'] for i in cat['items']}


def test_signage_page_and_menu(app, store_settings):
    tid = default_tenant_id(app)
    ok_id = make_item(app, '看板牛肉麵', price=180, stock=10, category='看板主餐', is_new=True, name_en='Beef Noodles')
    sold_id = make_item(app, '看板滷肉飯', price=60, stock=0, category='看板主餐', is_sold_out=True)
    disc_id = make_item(app, '看板紅茶', price=40, stock=10, category='看板飲料', is_discount=True, discount_price=30)
    make_item(app, '看板停售品', price=99, stock=10, category='看板飲料', sale_until=date(2000, 1, 1))
    c = _board(app)

    r = c.get('/signage')
    assert r.status_code == 200
    assert '數位看板' in r.get_data(as_text=True)

    r = c.get('/api/signage')
    assert r.status_code == 200 and r.headers.get('ETag')
    data = r.get_json()
    assert data['success'] is True
    items = _items_by_name(data)
    assert items['看板牛肉麵']['available'] is True
    assert items['看板牛肉麵']['is_new'] is True and items['看板牛肉麵']['name_en'] == 'Beef Noodles'
    assert items['看板滷肉飯']['available'] is False and items['看板滷肉飯']['hint'] == ''
    assert items['看板紅茶']['price'] == 30 and items['看板紅茶']['original_price'] == 40
    assert '看板停售品' not in items   # 已過販售期間的餐點不上看板
    assert data['pickup'] is not None and 'ready' in data['pickup']
    assert data['intake']['paused'] is False

    # ETag：內容沒變回 304
    assert c.get('/api/signage', headers={'If-None-Match': r.headers['ETag']}).status_code == 304

    # 售完隱藏、指定分類順序、不顯示英文、關閉叫號
    store_settings('signage', hide_sold_out=True, categories='看板飲料, 看板主餐', show_english=False, show_pickup=False)
    data = c.get('/api/signage').get_json()
    names = [cat['name'] for cat in data['categories']]
    assert names[:2] == ['看板飲料', '看板主餐'] and len(names) == 2
    items = _items_by_name(data)
    assert '看板滷肉飯' not in items and items['看板牛肉麵']['name_en'] == ''
    assert data['pickup'] is None

    # 恢復供應後即時反映 (看板只靠 /api/signage 重新抓取)
    with app.app_context():
        from app.extensions import db
        from app.models.menu import MenuItem
        item = db.session.get(MenuItem, sold_id)
        item.is_sold_out, item.stock = False, 5
        db.session.commit()
    assert '看板滷肉飯' in _items_by_name(c.get('/api/signage').get_json())

    # 標題與欄數等設定帶到頁面
    store_settings('signage', headline='今日菜單', columns=2)
    page = c.get('/signage').get_data(as_text=True)
    assert '今日菜單' in page and 'data-columns="2"' in page

    # 關閉看板：頁面與 API 都 404
    store_settings('signage', enabled=False)
    assert c.get('/signage').status_code == 404
    assert c.get('/api/signage').status_code == 404
    assert tid and ok_id and disc_id


def test_signage_promos_and_pickup(app, store_settings, staff_client):
    from app.extensions import db
    from app.models.promotion import Promotion
    c = _board(app)
    tid = default_tenant_id(app)

    # 沒填標語時輪播進行中的促銷活動名稱 (停用、只限線上的不顯示)
    with app.app_context():
        db.session.add_all([
            Promotion(tenant_id=tid, name='看板買一送一', kind='bogo', is_active=True, priority=5),
            Promotion(tenant_id=tid, name='看板停用活動', kind='bogo', is_active=False),
            Promotion(tenant_id=tid, name='看板線上限定', kind='bogo', is_active=True, channels='online'),
        ])
        db.session.commit()
    promos = c.get('/api/signage').get_json()['promos']
    assert '看板買一送一' in promos and '看板停用活動' not in promos and '看板線上限定' not in promos

    # 後台填的標語優先 (一行一則)
    store_settings('signage', promo_lines='午餐時段 11:00-14:00 套餐 9 折\n加入會員送紅茶')
    assert c.get('/api/signage').get_json()['promos'] == ['午餐時段 11:00-14:00 套餐 9 折', '加入會員送紅茶']

    # 候位叫號出現在看板
    r = c.post('/waitlist', data={'name': '看板候位', 'phone': '', 'party_size': '2'})
    assert r.status_code == 302
    with app.app_context():
        from app.models.reservation import WaitlistEntry
        entry = WaitlistEntry.query.filter_by(tenant_id=tid, name='看板候位').order_by(WaitlistEntry.id.desc()).first()
        eid, label = entry.id, entry.label
    staff_client.post('/reservations', data={'action': 'waitlist_status', 'entry_id': eid, 'status': 'called'})
    called = c.get('/api/signage').get_json()['pickup']['called']
    assert label in [w['label'] for w in called]
    staff_client.post('/reservations', data={'action': 'waitlist_status', 'entry_id': eid, 'status': 'seated', 'table_name': 'B9'})
    assert label not in [w['label'] for w in c.get('/api/signage').get_json()['pickup']['called']]


def test_signage_menu_edit_notifies_boards(app, admin_client, monkeypatch):
    """後台修改 / 刪除餐點會發出即時通知 (看板與點餐機訂閱 /api/orders_stream)"""
    import app.routes.admin as admin_routes
    calls = []
    monkeypatch.setattr(admin_routes.order_event_bus, 'notify', lambda tenant_id: calls.append(tenant_id))
    item_id = make_item(app, '看板通知測試', price=50, stock=5)
    r = admin_client.post(f'/admin/edit/{item_id}', data={'name': '看板通知測試', 'price': '50', 'stock': '0', 'category': '主餐',
                                                         'is_sold_out': 'on'})
    assert r.status_code == 302
    assert calls == [default_tenant_id(app)]
    with app.app_context():
        from app.models.menu import MenuItem
        from app.extensions import db
        assert db.session.get(MenuItem, item_id).is_sold_out is True
    admin_client.post(f'/admin/delete/{item_id}')
    assert len(calls) == 2
