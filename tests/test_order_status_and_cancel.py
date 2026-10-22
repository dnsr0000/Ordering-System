"""訂單狀態流轉 (廚房 / 櫃檯 / 取餐)、不合法轉換、各角色權限、取消時回補庫存 / 優惠券 / 點數、換日自動結案"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.tenant import Tenant
from app.models.user import StaffAuditLog, User
from app.services import order_service
from tests.support import (
    cancel_order,
    checkout,
    default_tenant_id,
    guest_client,
    line,
    login_staff,
    make_item,
    make_member,
    pos_checkout,
)

STATUSES = ['Pending', 'Completed', 'PickedUp', 'Cancelled']


LEGAL = {'Pending': {'Completed', 'Cancelled'}, 'Completed': {'PickedUp', 'Cancelled'}, 'PickedUp': {'Cancelled'}, 'Cancelled': set()}


def set_status(client, order_id, status):
    return client.post(f'/api/orders/{order_id}/status', json={'status': status})


def guest_order(app, item_id):
    r = checkout(guest_client(app), [line(item_id)])
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['order_id']


def old_order(app, status, **kw):
    """建立於昨天的訂單 (不影響今日取餐號與營運資料)"""
    with app.app_context():
        o = Order(tenant_id=default_tenant_id(app), table_number=kw.pop('table', '測試'), total_price=10, status=status,
                  pickup_number=1, created_at=datetime.now() - timedelta(days=1), **kw)
        db.session.add(o)
        db.session.commit()
        return o.id


def test_kitchen_counter_pickup_flow(app, kitchen_client, staff_client):
    """廚房出餐 → 公開叫號看板顯示 → 櫃檯核銷取餐 (含稽核)"""
    orders = kitchen_client.get('/api/kitchen_orders').get_json()['orders']
    assert orders
    oid = orders[0]['id']
    assert set_status(kitchen_client, oid, 'Completed').status_code == 200
    board = app.test_client().get('/api/counter_orders').get_json()
    assert oid in [o['id'] for o in board['ready_orders']]
    assert set_status(staff_client, oid, 'PickedUp').status_code == 200
    with app.app_context():
        assert db.session.get(Order, oid).status == 'PickedUp'
        assert StaffAuditLog.query.filter_by(action_type='MARK_PICKED_UP', target_order_id=oid).first()


def test_illegal_transitions_return_409(app, staff_client, manager_client, admin_client, ctx):
    """未出餐直接取餐、重複取消、已取消的訂單復活 → 409"""
    assert set_status(staff_client, ctx['pos_order_id'], 'PickedUp').status_code == 409
    oid = guest_order(app, ctx['menu_ids']['錫蘭紅茶'])
    assert set_status(manager_client, oid, 'Cancelled').status_code == 200
    assert set_status(manager_client, oid, 'Cancelled').status_code == 409
    assert set_status(manager_client, oid, 'Completed').status_code == 409
    assert set_status(admin_client, oid, 'Pending').status_code == 409
    assert set_status(manager_client, oid, 'PickedUp').status_code == 409


def test_cancel_rolls_back_everything(app, admin_client, ctx):
    """取消 POS 訂單：主餐與套餐配餐庫存回補、會員折抵點數退回且追回贈點、稽核"""
    ids, oid = ctx['menu_ids'], ctx['pos_order_id']
    names = ['招牌和牛堡', '美式脆薯', '錫蘭紅茶']
    with app.app_context():
        before = {n: db.session.get(MenuItem, ids[n]).stock for n in names}
        pts_before = User.query.filter_by(phone='0912999888').first().points
    assert set_status(admin_client, oid, 'Cancelled').status_code == 200
    with app.app_context():
        assert all(db.session.get(MenuItem, ids[n]).stock == before[n] + 1 for n in names)
        assert User.query.filter_by(phone='0912999888').first().points == pts_before + 25 - 3
        audit = StaffAuditLog.query.filter_by(action_type='CANCEL_ORDER', target_order_id=oid).first()
        assert audit.points_delta == 22


def test_kitchen_complete_all(app, kitchen_client):
    r = kitchen_client.post('/api/kitchen_complete_all')
    assert r.status_code == 200 and r.get_json()['success'] is True
    with app.app_context():
        assert Order.query.filter_by(status='Pending').count() == 0


def test_guest_order_cancel_once(app, admin_client, ctx):
    oid = guest_order(app, ctx['menu_ids']['錫蘭紅茶'])
    assert set_status(admin_client, oid, 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(Order, oid).status == 'Cancelled'
    assert set_status(admin_client, oid, 'Cancelled').status_code == 409


def test_kitchen_cannot_pick_up(app, kitchen_client):
    with app.app_context():
        oid = Order.query.filter_by(status='Completed').first().id
    assert set_status(kitchen_client, oid, 'PickedUp').status_code == 403


def test_staff_cannot_cancel_picked_up(app, staff_client):
    with app.app_context():
        oid = Order.query.filter_by(status='PickedUp').first().id
    assert set_status(staff_client, oid, 'Cancelled').status_code == 403


def test_cancel_reward_order_refunds_points(app, staff_client, admin_client, ctx):
    """POS 當場以點數兌換的商品，取消訂單後點數全額退回"""
    with app.app_context():
        user = User.query.filter_by(phone='0912345678').first()
        uid, init_pts = user.id, user.points
    r = checkout(staff_client, [line(ctx['menu_ids']['黃金雞塊'], is_reward=True)], url='/api/pos/checkout',
                 user_id=uid, table_number='退點測試桌')
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid).points == init_pts - 50
    assert set_status(admin_client, r.get_json()['order_id'], 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).points == init_pts


def test_concurrent_cancel_only_once(app):
    """8 個執行緒同時取消同一張單：只有 1 次成功，其餘 409；庫存與稽核只處理一次"""
    item_id = make_item(app, 'CAS併發取消測試餐', price=50, stock=9, total_stock=10)
    oid = old_order(app, 'Pending', table='CAS併發取消')
    with app.app_context():
        db.session.add(OrderItem(tenant_id=default_tenant_id(app), order_id=oid, menu_item_id=item_id,
                                 item_name='CAS併發取消測試餐', price=50, quantity=1))
        db.session.commit()
    clients = [login_staff(app, 'mgr_user', 'manager') for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda c: set_status(c, oid, 'Cancelled').status_code, clients))
    assert sorted(codes) == [200] + [409] * 7, codes
    with app.app_context():
        assert db.session.get(Order, oid).status == 'Cancelled'
        assert db.session.get(MenuItem, item_id).stock == 10
        assert StaffAuditLog.query.filter_by(action_type='CANCEL_ORDER', target_order_id=oid).count() == 1


def test_role_transition_matrix(app, admin_client, manager_client, staff_client, kitchen_client):
    """4 角色 × 16 種 (目前狀態 → 目標狀態) 逐一比對 200 / 403 / 409 與狀態是否改變"""
    all_pairs = {(c, t) for c, ts in LEGAL.items() for t in ts}
    allowed = {'owner': all_pairs, 'manager': all_pairs, 'staff': all_pairs - {('PickedUp', 'Cancelled')},
               'kitchen': {('Pending', 'Completed')}}
    clients = {'owner': admin_client, 'manager': manager_client, 'staff': staff_client, 'kitchen': kitchen_client}
    reachable = {t for ts in LEGAL.values() for t in ts}

    def expected(role, cur, tgt):
        if tgt not in reachable:
            return 409
        if tgt not in {t for _, t in allowed[role]}:
            return 403
        if tgt not in LEGAL[cur]:
            return 409
        return 200 if (cur, tgt) in allowed[role] else 403

    problems = []
    for role, client in clients.items():
        for cur in STATUSES:
            for tgt in STATUSES:
                oid = old_order(app, cur, table=f'矩陣{role}')
                code = set_status(client, oid, tgt).status_code
                with app.app_context():
                    after = db.session.get(Order, oid).status
                exp = expected(role, cur, tgt)
                if code != exp or after != (tgt if exp == 200 else cur):
                    problems.append(f'[{role}] {cur}->{tgt}: {code}/{after} (預期 {exp})')
    assert not problems, problems[:8]


def test_no_status_probing_without_permission(app, kitchen_client):
    """無權限角色不論訂單狀態或是否存在，都得到相同的 403 回應，訂單不變"""
    probe = {st: old_order(app, st, table='探測測試') for st in STATUSES}
    responses = {st: set_status(kitchen_client, oid, 'Cancelled') for st, oid in probe.items()}
    responses['missing'] = set_status(kitchen_client, 987654321, 'Cancelled')
    assert {r.status_code for r in responses.values()} == {403}
    assert len({r.get_json()['message'] for r in responses.values()}) == 1
    with app.app_context():
        assert {st: db.session.get(Order, oid).status for st, oid in probe.items()} == {st: st for st in probe}


def test_cancel_restores_only_applied_coupon(app, admin_client):
    tid = default_tenant_id(app)
    iid = make_item(app, '模組15券品', price=100, stock=50)
    uid = make_member(app, '模組15券會員', '0933150002')
    with app.app_context():
        cp = Coupon(tenant_id=tid, code='M15C', title='M15券', discount_type='fixed', discount_value=10, min_spend=0,
                    reward_points=5, is_reward=True)
        db.session.add(cp)
        db.session.flush()
        uc = UserCoupon(tenant_id=tid, user_id=uid, coupon_id=cp.id, code='M15C')
        db.session.add(uc)
        db.session.commit()
        ucid = uc.id
    a, b = pos_checkout(admin_client, iid, uid, 'M15C').get_json(), pos_checkout(admin_client, iid, uid, 'M15C').get_json()
    with app.app_context():
        oa, ob = db.session.get(Order, a['order_id']), db.session.get(Order, b['order_id'])
        assert (oa.coupon_code, oa.applied_user_coupon_id) == ('M15C', ucid)
        assert (ob.coupon_code, ob.applied_user_coupon_id) == ('', None)
    cancel_order(admin_client, b['order_id'])
    with app.app_context():
        assert db.session.get(UserCoupon, ucid).is_used is True, '取消沒有套用券的訂單不應復活別張已用券'
    cancel_order(admin_client, a['order_id'])
    with app.app_context():
        assert db.session.get(UserCoupon, ucid).is_used is False


def test_cancel_clawback_is_capped_and_audited(app, admin_client):
    iid = make_item(app, '模組15點數品', price=1000, stock=50)
    uid = make_member(app, '模組15點數會員', '0933150003')
    r = pos_checkout(admin_client, iid, uid).get_json()
    earned = r['points_earned']
    assert earned >= 4
    left = earned - 3
    with app.app_context():
        db.session.get(User, uid).points = left
        db.session.commit()
    assert cancel_order(admin_client, r['order_id']).status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).points == 0
        audit = StaffAuditLog.query.filter_by(action_type='CANCEL_ORDER', target_order_id=r['order_id']).first()
        assert '無法追回' in audit.details and audit.points_delta == -left


def test_daily_auto_close(app):
    tid = default_tenant_id(app)
    old = datetime.now() - timedelta(days=1, hours=2)
    with app.app_context():
        t2 = Tenant(name='M15結案店', slug='m15-ac')
        db.session.add(t2)
        db.session.flush()
        rows = {
            'old_a': Order(tenant_id=tid, table_number='M15自動結案', total_price=10, status='Pending', pickup_number=1, created_at=old),
            'old_other': Order(tenant_id=t2.id, table_number='M15自動結案', total_price=10, status='Pending', pickup_number=1,
                               created_at=datetime.now() - timedelta(days=5)),
            'today': Order(tenant_id=tid, table_number='M15自動結案', total_price=10, status='Pending', pickup_number=1,
                           created_at=datetime.now() - timedelta(minutes=1)),
            'old_picked': Order(tenant_id=tid, table_number='M15自動結案', total_price=10, status='PickedUp', pickup_number=1, created_at=old),
            'old_cancel': Order(tenant_id=tid, table_number='M15自動結案', total_price=10, status='Cancelled', pickup_number=1, created_at=old),
        }
        db.session.add_all(rows.values())
        db.session.commit()
        ids, tid2 = {k: v.id for k, v in rows.items()}, t2.id

        closed = order_service.auto_close_stale_orders()
        assert closed.get(tid) >= 1 and closed.get(tid2) == 1
        got = {k: db.session.get(Order, i) for k, i in ids.items()}
        assert got['old_a'].status == 'Completed' and got['old_a'].completed_at is None
        assert got['old_other'].status == 'Completed' and got['today'].status == 'Pending'
        assert (got['old_picked'].status, got['old_cancel'].status) == ('PickedUp', 'Cancelled')
        audits = StaffAuditLog.query.filter(StaffAuditLog.action_type == 'AUTO_CLOSE_STALE_ORDERS',
                                            StaffAuditLog.tenant_id.in_([tid, tid2])).all()
        assert {a.tenant_id for a in audits} == {tid, tid2} and {a.staff_username for a in audits} == {'system'}
        assert order_service.auto_close_stale_orders() == {}
        late = Order(tenant_id=tid, table_number='M15自動結案', total_price=10, status='Pending', pickup_number=1, created_at=old)
        db.session.add(late)
        db.session.commit()
        late_id = late.id
    order_service._auto_close_done_for['date'] = None
    app.test_client().get('/api/counter_orders')  # 跨日不重啟：第一個請求觸發
    with app.app_context():
        assert db.session.get(Order, late_id).status == 'Completed'
    assert order_service._auto_close_done_for['date'] == date.today()
