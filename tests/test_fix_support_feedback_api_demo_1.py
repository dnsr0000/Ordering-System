"""修正小組 support-feedback-api-demo-1 的回歸測試：對外 API v1、顧客評價、客服對話、示範資料"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.errors import AppError
from app.extensions import db, limiter
from app.models.integration import ApiKey
from app.models.menu import MenuItem
from app.models.order import Order, OrderFeedback
from app.models.support import SupportChat
from app.models.sync import NodeStock, SyncNode
from app.models.tenant import Tenant
from tests.support import get_or_create_tenant, tenant_request


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def _key(app, tid, scopes):
    with tenant_request(app, tid):
        key = ApiKey(tenant_id=tid, name='fix-sfad1', scopes=','.join(scopes))
        raw = key.issue()
        db.session.add(key)
        db.session.commit()
    return {'Authorization': f'Bearer {raw}'}


def _item(tid, name, price=100, **kw):
    item = MenuItem(tenant_id=tid, name=name, price=price, stock=kw.pop('stock', 999), total_stock=999, **kw)
    db.session.add(item)
    db.session.commit()
    return item.id


def _buy(tid, item_id, qty=1):
    from app.services.order_service import process_checkout
    order, _ = process_checkout(tenant_id=tid, items=[{'menu_item_id': item_id, 'quantity': qty}])
    db.session.commit()
    return order


# ---------------------------------------------------------------------------
# B-467：同一筆交易改到的訂單比一頁多時，cursor 分頁不可跳過
# ---------------------------------------------------------------------------
def test_cursor_feed_does_not_skip_orders_of_same_commit(app, no_rate_limit):
    tid = get_or_create_tenant(app, 'sfad1-feed')
    h = _key(app, tid, ['orders:read'])
    with tenant_request(app, tid):
        item_id = _item(tid, '游標餐')
        uuids = [_buy(tid, item_id).uuid for _ in range(5)]
    c = app.test_client()
    first = c.get('/api/v1/orders?cursor=0&limit=100', headers=h).get_json()
    cursor = first['next_cursor']
    assert isinstance(cursor, int) and first['has_more'] is False
    with tenant_request(app, tid):   # 一次提交改 5 張 (同一個 sync_rev)
        for o in Order.query.filter(Order.uuid.in_(uuids)).all():
            o.status = 'Completed'
        db.session.commit()
    seen, pages = [], 0
    while True:
        page = c.get('/api/v1/orders', query_string={'cursor': cursor, 'limit': 2}, headers=h).get_json()
        seen += [o['uuid'] for o in page['orders']]
        cursor, pages = page['next_cursor'], pages + 1
        if not page['has_more']:
            break
        assert pages < 10
    assert sorted(seen) == sorted(uuids) and len(seen) == 5
    assert c.get('/api/v1/orders', query_string={'cursor': cursor}, headers=h).get_json()['orders'] == []
    # B-475：超大整數回 400 (不是 500)
    assert c.get('/api/v1/orders?cursor=99999999999999999999', headers=h).status_code == 400
    assert c.get('/api/v1/orders?since=2026-01-01T00:00:00&after_id=99999999999999999999', headers=h).status_code == 400
    # B-471：依文件帶小寫狀態也能篩選
    done = c.get('/api/v1/orders?status=completed', headers=h).get_json()['orders']
    assert {o['uuid'] for o in done} == set(uuids) and all(o['status'] == 'Completed' for o in done)


# ---------------------------------------------------------------------------
# R2-182：cursor 依 feed_rev 追蹤；從線上端套用進來的訂單異動 (只更新 feed_rev、不標記 sync_rev) 也收得到
# ---------------------------------------------------------------------------
def test_cursor_feed_follows_feed_rev_for_synced_changes(app, no_rate_limit):
    from types import SimpleNamespace

    from app.sync.node import NodeSync
    tid = get_or_create_tenant(app, 'sfad1-feedrev')
    h = _key(app, tid, ['orders:read'])
    with tenant_request(app, tid):
        order = _buy(tid, _item(tid, '同步異動餐'))
        order_id, order_uuid, sync_before = order.id, order.uuid, order.sync_rev
    c = app.test_client()
    cursor = c.get('/api/v1/orders?cursor=0&limit=500', headers=h).get_json()['next_cursor']
    assert c.get('/api/v1/orders', query_string={'cursor': cursor}, headers=h).get_json()['orders'] == []
    with tenant_request(app, tid):   # 模擬從線上端套用進來的異動：只更新 feed_rev
        NodeSync._bump_feed_rev(SimpleNamespace(changed_order_ids={order_id}, new_orders=[]))
        db.session.commit()
        assert db.session.get(Order, order_id).sync_rev == sync_before
    page = c.get('/api/v1/orders', query_string={'cursor': cursor}, headers=h).get_json()
    assert [o['uuid'] for o in page['orders']] == [order_uuid] and page['next_cursor'] > cursor


# ---------------------------------------------------------------------------
# B-468：since / until 帶時區時換算為本機時間
# ---------------------------------------------------------------------------
def test_since_with_timezone_is_converted_to_local(app, no_rate_limit):
    tid = get_or_create_tenant(app, 'sfad1-tz')
    h = _key(app, tid, ['orders:read'])
    with tenant_request(app, tid):
        item_id = _item(tid, '時區餐')
        early, late = _buy(tid, item_id), _buy(tid, item_id)
        early.created_at = datetime(2026, 11, 8, 10, 0)
        late.created_at = datetime(2026, 11, 8, 14, 0)
        db.session.commit()
        late_uuid = late.uuid
    noon_utc = datetime(2026, 11, 8, 12, 0).astimezone().astimezone(timezone.utc)
    since = noon_utc.replace(tzinfo=None).isoformat() + 'Z'
    c = app.test_client()
    rows = c.get('/api/v1/orders', query_string={'since': since, 'until': '2026-11-08T23:59:59'}, headers=h).get_json()
    assert [o['uuid'] for o in rows['orders']] == [late_uuid]


# ---------------------------------------------------------------------------
# B-469 / R2-184 / B-470 / B-476：菜單價格、供應時段、補貨解除售完、sold_out 型別
# ---------------------------------------------------------------------------
def test_menu_price_service_status_and_availability(app, no_rate_limit):
    tid = get_or_create_tenant(app, 'sfad1-menu')
    h = _key(app, tid, ['menu:read', 'menu:write'])
    with tenant_request(app, tid):
        promo = _item(tid, '特價餐', 150, is_discount=True, discount_price=99)
        ended = _item(tid, '已停售餐', 80, sale_until=date.today() - timedelta(days=7))
        auto = _item(tid, '賣完餐', 60, stock=0, is_sold_out=True)
        manual = _item(tid, '手動售完餐', 60, stock=10, is_sold_out=True)
    c = app.test_client()
    items = {m['id']: m for m in c.get('/api/v1/menu', headers=h).get_json()['items']}
    assert (items[promo]['price'], items[promo]['list_price']) == (99, 150)
    assert items[ended]['is_available'] is False and items[ended]['service_hint'] == '已停售'
    assert items[promo]['is_available'] is True and items[promo]['service_hint'] == ''
    r = c.post(f'/api/v1/menu/{auto}/availability', json={'stock': 20}, headers=h).get_json()
    assert r['item'] == {'id': auto, 'stock': 20, 'is_sold_out': False}
    r = c.post(f'/api/v1/menu/{manual}/availability', json={'stock': 30}, headers=h).get_json()
    assert r['item']['is_sold_out'] is True                       # 店員手動標示的售完不變
    r = c.post(f'/api/v1/menu/{promo}/availability', json={'stock': 0}, headers=h).get_json()
    assert r['item']['is_sold_out'] is True
    r = c.post(f'/api/v1/menu/{manual}/availability', json={'sold_out': 'false'}, headers=h).get_json()
    assert r['item']['is_sold_out'] is False
    assert c.post(f'/api/v1/menu/{manual}/availability', json={'sold_out': 'maybe'}, headers=h).status_code == 400


# ---------------------------------------------------------------------------
# R2-183：線上端的菜單庫存依分店回報；POST stock 回 400
# ---------------------------------------------------------------------------
def test_hub_menu_uses_node_stock(app, monkeypatch, no_rate_limit):
    tid = get_or_create_tenant(app, 'sfad1-hub')
    h = _key(app, tid, ['menu:read', 'menu:write'])
    with tenant_request(app, tid):
        item_id = _item(tid, '分店庫存餐', stock=50)
        node = SyncNode(tenant_id=tid, name='甲分店', token_hash='sfad1-hub-node')
        db.session.add(node)
        db.session.commit()
        db.session.add(NodeStock(node_id=node.id, menu_item_id=item_id, stock=0, is_sold_out=False))
        db.session.commit()
        node_uuid = node.uuid
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    c = app.test_client()
    whole = {m['id']: m for m in c.get('/api/v1/menu', headers=h).get_json()['items']}[item_id]
    assert whole['stock'] is None and whole['is_available'] is False
    one = {m['id']: m for m in c.get(f'/api/v1/menu?node={node_uuid}', headers=h).get_json()['items']}[item_id]
    assert (one['stock'], one['is_sold_out'], one['is_available']) == (0, True, False)
    assert c.get('/api/v1/menu?node=no-such-node', headers=h).status_code == 404
    assert c.post(f'/api/v1/menu/{item_id}/availability', json={'stock': 5}, headers=h).status_code == 400
    assert c.post(f'/api/v1/menu/{item_id}/availability', json={'sold_out': True}, headers=h).status_code == 200


# ---------------------------------------------------------------------------
# B-473 / R2-186：評價回覆只在內容改變時通知；rating=1e400 回 400
# ---------------------------------------------------------------------------
def test_feedback_reply_notifies_once_and_rating_overflow(app, monkeypatch):
    from app.services import feedback_service, push_service
    sent = []
    monkeypatch.setattr(push_service, 'notify_order', lambda order, kind: sent.append(kind))
    tid = get_or_create_tenant(app, 'sfad1-feedback')
    with tenant_request(app, tid):
        order = _buy(tid, _item(tid, '評價餐'))
        order.status = 'Completed'
        db.session.commit()
        with pytest.raises(AppError) as err:
            feedback_service.submit(order, float('inf'))
        assert err.value.status == 400
        fb = OrderFeedback(tenant_id=tid, order_id=order.id, rating=5, comment='好吃')
        db.session.add(fb)
        db.session.commit()
        feedback_service.reply(fb, '謝謝您！')
        feedback_service.reply(fb, '謝謝您！', is_public=True)     # 只切換公開
        assert sent == ['feedback_reply'] and fb.is_public is True
        feedback_service.reply(fb, '謝謝您，歡迎再來！')
        assert sent == ['feedback_reply', 'feedback_reply']


# ---------------------------------------------------------------------------
# B-474：較早建立的待回覆對話不可被擠出清單
# ---------------------------------------------------------------------------
def test_support_list_keeps_old_waiting_chat(app):
    from app.services import support_service
    tid = get_or_create_tenant(app, 'sfad1-support')
    with tenant_request(app, tid):
        base = datetime.now() - timedelta(days=3)
        old = SupportChat(tenant_id=tid, status='waiting', customer_label='舊顧客', created_at=base)
        db.session.add(old)
        db.session.add_all(SupportChat(tenant_id=tid, status='open', customer_label=f'顧客{i}',
                                       created_at=base + timedelta(minutes=i + 1)) for i in range(55))
        db.session.commit()
        rows = support_service.list_chats(limit=50)
        assert len(rows) == 50 and rows[0]['id'] == old.id


# ---------------------------------------------------------------------------
# B-477：demo seed 的 --slug 正規化
# ---------------------------------------------------------------------------
def test_demo_seed_normalizes_slug(app):
    from app.services import demo_service
    with app.app_context():
        with pytest.raises(demo_service.DemoError):
            demo_service.find_demo('!!!')
    runner = app.test_cli_runner()
    r = runner.invoke(args=['demo', 'seed', '--slug', 'SfadDemo', '--days', '1', '--orders-per-day', '2',
                            '--password', 'sfad1-demo-pass'])
    assert r.exit_code == 0, r.output
    assert '/shop/sfaddemo' in r.output
    with app.app_context():
        assert Tenant.query.filter_by(slug='sfaddemo').count() == 1
    r = runner.invoke(args=['demo', 'remove', '--slug', 'SfadDemo', '--yes'])
    assert r.exit_code == 0, r.output
    with app.app_context():
        assert Tenant.query.filter_by(slug='sfaddemo').count() == 0
