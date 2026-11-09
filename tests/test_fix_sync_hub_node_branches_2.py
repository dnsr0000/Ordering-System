"""修正小組 sync-hub-node-branches-2 的回歸測試 (線上端同步 API / 分店價格 / 叫貨調撥 / 分店管理)"""
import json
import uuid as uuidlib

import pytest

from app.errors import AppError
from app.extensions import db
from tests.support import default_tenant_id, flashes, login_staff, make_item, make_member, tenant_request


@pytest.fixture
def hub(app, monkeypatch):
    """暫時把測試 app 當成線上端 (hub)"""
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    return app


def _node(app, name, tid):
    from app.sync.nodes import create_node
    with tenant_request(app, tid):
        node, token = create_node(tid, name)
        return node.id, node.uuid, token


def _online_order(app, tid, node_uuid, item_id=None, qty=1, payment_status='paid', total=100, **kw):
    from app.models.enums import OrderSource, OrderStatus
    from app.models.order import Order, OrderItem
    with tenant_request(app, tid):
        order = Order(tenant_id=tid, total_price=total, status=kw.pop('status', OrderStatus.PENDING),
                      order_source=OrderSource.ONLINE, node_uuid=node_uuid, payment_status=payment_status,
                      pickup_number=0, **kw)
        db.session.add(order)
        db.session.flush()
        if item_id:
            db.session.add(OrderItem(order_id=order.id, menu_item_id=item_id, item_name='測試餐點', price=total,
                                     quantity=qty))
        db.session.commit()
        return order.id, order.uuid


# ---------------------------------------------------------------------------
# B-181：待付款訂單的兌換憑證 / 異動帳不可先送到分店
# ---------------------------------------------------------------------------
def test_b181_ledger_of_unpaid_online_order_not_pulled_until_paid(hub):
    from app.models.order import Order
    from app.models.sync import PointLedger
    from app.sync import tracking
    from app.sync.engine import collect_changes
    from app.sync.hub import _order_filter
    from app.sync.registry import BY_NAME
    tid = default_tenant_id(hub)
    _, node_uuid, _ = _node(hub, 'B181 分店', tid)
    uid = make_member(hub, 'B181 會員', '0955181181', points=100)
    oid, _ = _online_order(hub, tid, node_uuid, payment_status='pending')
    with tenant_request(hub, tid):
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=-30, reason='reward_item', order_id=oid))
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=5, reason='adjust'))   # 與訂單無關：照常下發
        db.session.commit()
        # 直接新增異動帳後要重算會員點數，否則 User.points 與異動帳合計不一致 (會讓後續的不變量檢查失敗)
        from app.services.points_service import recompute_points
        recompute_points([uid])
        db.session.commit()
        specs = (BY_NAME['point_ledger'],)
        from app.models.user import User
        user_refs = {uid, db.session.get(User, uid).uuid}   # 同步資料的 user_id 可能已換成 uuid

        def pulled():
            # 只看這位會員的異動帳：預設店家裡其他測試留下的異動帳 (例如其他兌換憑證 -30) 也在同一批同步資料裡
            rev = tracking.current_rev(db.session)
            changes, _ = collect_changes(tid, specs, -1, rev, row_filter=_order_filter(node_uuid), limit=100000)
            return [r for r in changes.get('point_ledger', []) if r.get('user_id') in user_refs]

        rows = pulled()
        assert all(r.get('reason') != 'reward_item' for r in rows if r.get('delta') == -30)
        assert any(r.get('reason') == 'adjust' and r.get('delta') == 5 for r in rows)

        # 付款完成後 (mark_paid 會重新標記這些列) 跟著訂單一起送出
        db.session.get(Order, oid).payment_status = 'paid'
        db.session.commit()
        assert any(r.get('reason') == 'reward_item' and r.get('delta') == -30 for r in pulled())


# ---------------------------------------------------------------------------
# B-183：到店付款的線上訂單也要從庫存回報中扣掉
# ---------------------------------------------------------------------------
def test_b183_pay_at_store_online_orders_count_as_in_flight(hub):
    from app.models.sync import SyncNode
    from app.sync.hub import _in_flight_demands
    tid = default_tenant_id(hub)
    _, node_uuid, _ = _node(hub, 'B183 分店', tid)
    item_id = make_item(hub, 'B183 限量便當', stock=10)
    _online_order(hub, tid, node_uuid, item_id, qty=2, payment_status='unpaid')
    _online_order(hub, tid, node_uuid, item_id, qty=1, payment_status='')
    _online_order(hub, tid, node_uuid, item_id, qty=4, payment_status='expired')        # 不會出餐
    _online_order(hub, tid, node_uuid, item_id, qty=8, payment_status='refund_required')
    with tenant_request(hub, tid):
        node = SyncNode.query.filter_by(uuid=node_uuid).first()
        assert _in_flight_demands(node).get(item_id) == 3


# ---------------------------------------------------------------------------
# B-182 / R2-069：分店取消 / 部分退款線上訂單後，線上端的發票作廢 / 折讓；回報庫存照常
# ---------------------------------------------------------------------------
def _encoded_order(app, tid, oid, **changes):
    from app.models.order import Order
    from app.sync.engine import encode_rows
    from app.sync.registry import BY_NAME
    with tenant_request(app, tid):
        table = Order.__table__
        rows = db.session.execute(db.select(table).where(table.c.id == oid)).mappings().all()
        row = encode_rows(BY_NAME['order'], rows)[0]
    row.update(changes)
    return json.loads(json.dumps(row, default=str))


def test_b182_branch_cancel_or_refund_adjusts_hub_invoices(hub, monkeypatch):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.models.menu import MenuItem
    from app.models.sync import NodeStock
    from app.services import payment_service
    tid = default_tenant_id(hub)
    node_id, node_uuid, token = _node(hub, 'B182 分店', tid)
    item_id = make_item(hub, 'B182 餐點', stock=10)
    cancelled_id, _ = _online_order(hub, tid, node_uuid, item_id, payment_status='paid', total=100)
    partial_id, _ = _online_order(hub, tid, node_uuid, item_id, payment_status='paid', total=200)
    at_store_id, _ = _online_order(hub, tid, node_uuid, item_id, payment_status='settled', total=80)
    calls = []
    monkeypatch.setattr(payment_service, '_void_invoices', lambda order, amount=0: calls.append(('void', order.id, amount)))
    monkeypatch.setattr(payment_service, '_refund_order_invoices',
                        lambda order, amount, reason, method=None: calls.append(('allowance', order.id, amount)))
    with tenant_request(hub, tid):
        item_uuid = db.session.get(MenuItem, item_id).uuid
    changes = {'order': [
        _encoded_order(hub, tid, cancelled_id, status=OrderStatus.CANCELLED, payment_status='refund_required',
                       refunded_amount=100, pickup_number=7),
        _encoded_order(hub, tid, partial_id, refunded_amount=30, pickup_number=8),
        _encoded_order(hub, tid, at_store_id, status=OrderStatus.CANCELLED, refunded_amount=80, pickup_number=9),
    ]}
    resp = hub.test_client().post('/api/sync/v1/push', headers={'Authorization': f'Bearer {token}'},
                                  json={'changes': changes, 'stock': [{'uuid': item_uuid, 'stock': 6}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert sorted(calls) == sorted([('void', cancelled_id, 100), ('allowance', partial_id, 30)])
    with tenant_request(hub, tid):
        assert db.session.get(Order, cancelled_id).status == OrderStatus.CANCELLED
        assert db.session.get(NodeStock, (node_id, item_id)).stock == 6   # 三張都已送達分店 (有取餐號)


# ---------------------------------------------------------------------------
# R2-066：分店價差不可讓會員價 / 未開啟的特價變成 0 元以下
# ---------------------------------------------------------------------------
def test_r2_066_branch_price_checks_member_and_inactive_discount_price(hub):
    from app.services import branch_service
    tid = default_tenant_id(hub)
    _, node_uuid, _ = _node(hub, 'R2066 分店', tid)
    latte = make_item(hub, 'R2066 拿鐵', price=120, member_price=60)
    combo = make_item(hub, 'R2066 套餐', price=100, discount_price=35, is_discount=False)
    with tenant_request(hub, tid):
        with pytest.raises(AppError, match='會員價'):
            branch_service.save_overrides(tid, node_uuid, [{'menu_item_id': latte, 'price': 55}])
        db.session.rollback()
        with pytest.raises(AppError, match='特價'):
            branch_service.save_overrides(tid, node_uuid, [{'menu_item_id': combo, 'price': 60}])
        db.session.rollback()
        changed, _ = branch_service.save_overrides(tid, node_uuid, [{'menu_item_id': latte, 'price': 110}])
        assert changed == 1
        db.session.rollback()


# ---------------------------------------------------------------------------
# B-191：叫貨 / 調撥的同名原料單位不同時擋下
# ---------------------------------------------------------------------------
def test_b191_ship_rejects_unit_mismatch(hub):
    from app.models.branch import HQ_SITE, StockTransfer
    from app.models.inventory import Ingredient
    from app.services import branch_service
    tid = default_tenant_id(hub)
    name = f'B191 麵粉 {uuidlib.uuid4().hex[:6]}'
    with tenant_request(hub, tid):
        ing = Ingredient(tenant_id=tid, name=name, unit='kg', stock=1000)
        db.session.add(ing)
        tr = StockTransfer(tenant_id=tid, kind='request', from_site=HQ_SITE, to_site=str(uuidlib.uuid4()),
                           from_name='總部', to_name='分店', status='requested')
        tr.set_lines([{'name': name, 'unit': 'g', 'qty': 500, 'shipped': None, 'received': None, 'unit_cost': 0.1}])
        db.session.add(tr)
        db.session.commit()
        ing_id, tr_id = ing.id, tr.id
        with pytest.raises(AppError, match='單位不同'):
            branch_service.ship(tid, tr_id, {}, staff='測試')
        db.session.rollback()
        assert db.session.get(Ingredient, ing_id).stock == 1000
        assert db.session.get(StockTransfer, tr_id).status == 'requested'

    branch_service._check_unit({'unit': 'KG '}, Ingredient(name='x', unit='kg'))   # 大小寫 / 空白不同視為相同
    branch_service._check_unit({'unit': ''}, Ingredient(name='x', unit='kg'))      # 舊單據沒有單位：不檢查


# ---------------------------------------------------------------------------
# R2-065：停用分店時提醒尚未送達的線上訂單
# ---------------------------------------------------------------------------
def test_r2_065_disable_node_warns_undelivered_online_orders(hub):
    tid = default_tenant_id(hub)
    node_id, node_uuid, _ = _node(hub, 'R2065 分店', tid)
    oid, _ = _online_order(hub, tid, node_uuid, payment_status='pending')
    client = login_staff(hub, 'r2065_owner', role='owner')
    resp = client.post('/admin/nodes', data={'action': 'toggle_active', 'node_id': node_id})
    assert resp.status_code == 302
    messages = ' '.join(flashes(client))
    assert f'#{oid}' in messages and '尚未送達' in messages
    from app.models.sync import SyncNode
    with tenant_request(hub, tid):
        assert db.session.get(SyncNode, node_id).is_active is False
    # 重新啟用不提醒
    client.post('/admin/nodes', data={'action': 'toggle_active', 'node_id': node_id})
    assert not any('尚未送達' in m for m in flashes(client))


# ---------------------------------------------------------------------------
# R2-067：hello 回傳線上端接受上傳的結構；push 的 backfill 補上舊版線上端略過的新欄位
# ---------------------------------------------------------------------------
def test_r2_067_hello_schema_and_push_backfill(hub):
    from app.models.menu import MenuItem
    from app.sync.engine import encode_rows
    from app.sync.registry import BY_NAME, synced_columns
    tid = default_tenant_id(hub)
    _, _, token = _node(hub, 'R2067 分店', tid)
    headers = {'Authorization': f'Bearer {token}'}
    client = hub.test_client()
    info = client.post('/api/sync/v1/hello', headers=headers).get_json()
    assert info['schema']['menu_item'] == synced_columns(BY_NAME['menu_item'])
    assert 'order' in info['schema']
    assert all(BY_NAME[name].direction != 'down' for name in info['schema'])

    item_id = make_item(hub, 'R2067 拿鐵', price=120)
    with tenant_request(hub, tid):
        table = MenuItem.__table__
        rows = db.session.execute(db.select(table).where(table.c.id == item_id)).mappings().all()
        row = json.loads(json.dumps(encode_rows(BY_NAME['menu_item'], rows)[0], default=str))
        before_at = db.session.get(MenuItem, item_id).updated_at
    row['member_price'] = 99          # 分店的值 (線上端舊版時被略過)，updated_at 與線上端相同
    row['price'] = 1                  # backfill 只補指定的欄位
    resp = client.post('/api/sync/v1/push', headers=headers, json={'changes': {'menu_item': [row]}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['schema_hash'] == info['schema_hash']
    with tenant_request(hub, tid):
        assert not db.session.get(MenuItem, item_id).member_price   # 一般套用：updated_at 相同不覆蓋
    resp = client.post('/api/sync/v1/push', headers=headers,
                       json={'changes': {'menu_item': [row]},
                             'backfill': {'menu_item': ['member_price', 'uuid'], 'staff_account': ['role']}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with tenant_request(hub, tid):
        item = db.session.get(MenuItem, item_id)
        assert item.member_price == 99 and item.price == 120 and item.updated_at == before_at
    # 線上端在這之後又改過的列：不被較舊的補值覆蓋
    row2 = dict(row, member_price=50, updated_at='2000-01-01T00:00:00')
    client.post('/api/sync/v1/push', headers=headers,
                json={'changes': {'menu_item': [row2]}, 'backfill': {'menu_item': ['member_price']}})
    with tenant_request(hub, tid):
        assert db.session.get(MenuItem, item_id).member_price == 99


# ---------------------------------------------------------------------------
# R2-001：公開代碼的每位會員限用次數在線上端事後比對
# ---------------------------------------------------------------------------
def test_r2_001_public_code_overuse_across_branches_alerts(hub, monkeypatch):
    from app.models.coupon import Coupon
    from app.models.enums import OrderSource, OrderStatus
    from app.models.order import Order
    from app.models.sync import SyncNode
    from app.services import monitor_service
    from app.sync.hub import _reconcile_public_code_use
    tid = default_tenant_id(hub)
    _, node_a, _ = _node(hub, 'R2001 A 分店', tid)
    _, node_b, _ = _node(hub, 'R2001 B 分店', tid)
    uid = make_member(hub, 'R2001 會員', '0955200101')
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda message, **kw: alerts.append((message, kw)))
    with tenant_request(hub, tid):
        db.session.add(Coupon(tenant_id=tid, code='R2001NEW', title='新客券', discount_type='fixed', discount_value=50,
                              is_reward=False, uses_per_member=1))
        orders = [Order(tenant_id=tid, user_id=uid, total_price=50, coupon_code='R2001NEW',
                        status=OrderStatus.COMPLETED, order_source=OrderSource.KIOSK, node_uuid=n)
                  for n in (node_a, node_b)]
        db.session.add_all(orders)
        db.session.commit()
        node = SyncNode.query.filter_by(uuid=node_b).first()
        _reconcile_public_code_use(node, [orders[1].uuid])
        assert len(alerts) == 1
        message, kw = alerts[0]
        assert 'R2001NEW' in message and '限用 1 次' in message and 'R2001 A 分店' in message and '使用 2 次' in message
        assert kw['key'] == f'promo-overuse-R2001NEW-{uid}' and kw['level'] == 'warning'
        # 其中一張取消後未超用：不通知
        orders[0].status = OrderStatus.CANCELLED
        db.session.commit()
        _reconcile_public_code_use(node, [orders[1].uuid])
        assert len(alerts) == 1


# ---------------------------------------------------------------------------
# B-179：同步前一邊退點、一邊核銷的兌換憑證，線上端追回退回的點數 (重送不重複扣)
# ---------------------------------------------------------------------------
def test_b179_reused_voucher_points_clawed_back_once(hub, monkeypatch):
    from app.models.enums import OrderSource, OrderStatus, VoucherStatus
    from app.models.order import Order
    from app.models.sync import PointLedger, SyncNode
    from app.models.user import RewardRedemption, User
    from app.services import monitor_service
    from app.sync.hub import _claw_back_reused_vouchers
    tid = default_tenant_id(hub)
    _, node_uuid, _ = _node(hub, 'B179 分店', tid)
    uid = make_member(hub, 'B179 會員', '0955179179', points=100)
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda message, **kw: alerts.append((message, kw)))
    with tenant_request(hub, tid):
        order = Order(tenant_id=tid, user_id=uid, total_price=0, status=OrderStatus.COMPLETED,
                      order_source=OrderSource.KIOSK, node_uuid=node_uuid)
        db.session.add(order)
        db.session.flush()
        v = RewardRedemption(tenant_id=tid, user_id=uid, points_cost=30, status=VoucherStatus.USED, order_id=order.id)
        db.session.add(v)
        db.session.commit()
        node = SyncNode.query.filter_by(uuid=node_uuid).first()
        assert _claw_back_reused_vouchers(node, [v, v]) == {uid}
        db.session.commit()
        assert db.session.get(User, uid).points == 70
        assert len(alerts) == 1 and '30 點' in alerts[0][0] and alerts[0][1]['key'] == f'voucher-reuse-{v.uuid}'
        assert _claw_back_reused_vouchers(node, [v]) == set()   # 重送同一頁：不重複扣點
        db.session.commit()
        assert db.session.get(User, uid).points == 70
        assert PointLedger.query.filter_by(user_id=uid, reason='voucher_reuse').count() == 1


# ---------------------------------------------------------------------------
# R2-002：分店回報本店生效的自有外送設定
# ---------------------------------------------------------------------------
def test_r2_002_save_own_delivery_summary():
    from app.models.sync import SyncNode
    from app.sync.hub import _save_own_delivery
    node = SyncNode(name='x', token_hash='x')
    node.own_delivery_json = 'keep'
    _save_own_delivery(node, None)            # 未回報：維持上次的值
    assert node.own_delivery_json == 'keep'
    _save_own_delivery(node, {'enabled': True, 'channels': ['pos', 'evil'], 'zones': '近:30:200',
                              'free_over': '-5', 'eta_minutes': 'x'})
    assert json.loads(node.own_delivery_json) == {'enabled': True, 'channels': ['pos'], 'zones': '近:30:200',
                                                  'free_over': 0, 'eta_minutes': 45}


def test_r2_002_intake_delivery_saved_and_used_by_online_checkout(app, store_settings):
    """分店 intake['delivery'] (node._intake_snapshot) 存成 own_delivery_json，線上端依該分店設定驗證與計費"""
    from app.models.sync import SyncNode
    from app.services import own_delivery_service as od
    from app.sync.hub import _save_intake
    store_settings('own_delivery', enabled=True, zones='總部區:99:0', free_over=0, channels=['pos', 'online'])
    node = SyncNode(name='x', token_hash='x')
    _save_intake(node, {'paused_until': '', 'message': ''})       # 舊版分店：沒有 delivery，維持未回報
    assert node.own_delivery_json is None
    _save_intake(node, {'paused_until': '', 'message': '', 'delivery': {
        'enabled': True, 'online': True, 'free_over': 800,
        'zones': [{'name': '3 公里:內', 'fee': 50, 'min_spend': 300}, {'name': '遠', 'fee': -9, 'min_spend': 'x'},
                  'bad', {'name': '', 'fee': 1}]}})
    with tenant_request(app, default_tenant_id(app)):
        assert od.zones(node=node) == [{'name': '3 公里 內', 'fee': 50, 'min_spend': 300},
                                       {'name': '遠', 'fee': 0, 'min_spend': 0}]
        assert od.enabled_for('online', node=node)
        assert od.fee_for('3 公里 內', 900, node=node) == 0          # 分店的免運門檻
        _save_intake(node, {'paused_until': '', 'message': '', 'delivery': {
            'enabled': True, 'online': False, 'zones': [], 'free_over': 0}})
        assert not od.enabled_for('online', node=node), '分店不開放線上外送時線上端也擋下'
        _save_intake(node, {'paused_until': '', 'message': ''})   # 未帶 delivery：維持上次的回報
        assert not od.enabled_for('online', node=node)
