"""第三輪 #7、#8、#10：分店價格 / 分店上下架、菜單工程分析、分店叫貨 / 中央廚房 / 分店間調撥"""
from datetime import date

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.branch import BranchMenuOverride, StockTransfer
from app.models.inventory import Ingredient, StockMovement
from app.models.menu import MenuItem
from tests.support import checkout, guest_client, line, login_staff, make_item, run_together


# ===========================================================================
# 8. 菜單工程分析
# ===========================================================================
def test_menu_engineering_four_quadrants_page_and_excel(app, admin_client):
    from app.services import menu_engineering_service
    cat = '工程分析測試'
    specs = {'明星餐': (200, 60, 10), '金牛餐': (100, 80, 10), '潛力餐': (220, 40, 2), '瘦狗餐': (90, 80, 2)}
    ids = {name: make_item(app, name, price=price, stock=100, category=cat, unit_cost=cost)
           for name, (price, cost, _qty) in specs.items()}
    client = guest_client(app)
    for name, (_price, _cost, qty) in specs.items():
        r = checkout(client, [line(ids[name], qty)])
        assert r.status_code == 200, r.get_json()
    # 取消的訂單不計
    r = checkout(client, [line(ids['瘦狗餐'], 5)])
    order_id = r.get_json()['order_id']
    assert admin_client.post(f'/api/orders/{order_id}/status', json={'status': 'Cancelled'}).status_code == 200

    with app.app_context():
        from app.models.tenant import Tenant
        tid = Tenant.query.filter_by(slug='default').first().id
        result = menu_engineering_service.analyze(tid, date.today(), date.today(), category=cat)
    by = {r['name']: r for r in result['items']}
    assert {n: by[n]['cls'] for n in specs} == {'明星餐': 'star', '金牛餐': 'plowhorse', '潛力餐': 'puzzle', '瘦狗餐': 'dog'}
    assert by['瘦狗餐']['qty'] == 2                        # 取消的 5 份不算
    assert result['pop_threshold'] == 4.2                 # 24 份 / 4 項 × 70%
    assert result['cm_threshold'] == 82.5                 # (1400 + 200 + 360 + 20) / 24
    assert by['明星餐']['unit_cm'] == 140 and by['金牛餐']['food_cost_pct'] == 80.0
    assert result['counts'] == {'star': 1, 'plowhorse': 1, 'puzzle': 1, 'dog': 1}

    page = admin_client.get(f'/admin/reports/menu-engineering?category={cat}')
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'id="meChart"' in html and html.count('data-me-point=') == 4
    assert '明星' in html and '金牛' in html
    xlsx = admin_client.get(f'/admin/reports/menu-engineering?category={cat}&format=xlsx')
    assert xlsx.status_code == 200 and xlsx.data[:2] == b'PK'
    # 一般員工沒有報表權限
    staff = login_staff(app, 'me_staff', role='staff')
    assert staff.get('/admin/reports/menu-engineering').status_code == 403


def test_branch_pages_in_standalone_mode(app, admin_client):
    assert '單機模式' in admin_client.get('/admin/branch-menu').get_data(as_text=True)
    page = admin_client.get('/admin/transfers').get_data(as_text=True)
    assert '單機模式請使用' in page
    r = admin_client.post('/admin/transfers', data={'action': 'create_request', 'site': 'HQ', 'line_name': ['x'],
                                                    'line_qty': ['1']}, follow_redirects=True)
    assert '需要啟用多分店同步' in r.get_data(as_text=True)
    settings = admin_client.get('/admin/settings').get_data(as_text=True)
    assert '菜單工程分析' in settings and '分店叫貨 / 調撥' in settings and '分店價格 / 上下架' in settings


# ===========================================================================
# 7 + 10. 多分店：分店價格 / 上下架、叫貨 / 調撥
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    from tests.test_sync_hub_node import Env
    e = Env()
    yield e
    e.close()


def _ing(app_, name, stock=None, unit_cost=None, unit='g'):
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(app_) as tenant:
        ing = Ingredient.query.filter_by(name=name).first()
        if ing is None and stock is not None:
            ing = Ingredient(tenant_id=tenant.id, name=name, unit=unit, stock=stock, unit_cost=unit_cost or 0)
            db.session.add(ing)
            db.session.commit()
        return None if ing is None else (round(ing.stock, 4), round(ing.unit_cost, 4), ing.unit)


def _tr(app_, uuid):
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(app_):
        t = StockTransfer.query.filter_by(uuid=uuid).first()
        return None if t is None else dict(id=t.id, status=t.status, lines=t.lines, reason=t.reason)


def _act(app_, fn, *args, **kw):
    from app.services import branch_service
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(app_) as tenant:
        tr = getattr(branch_service, fn)(tenant.id, *args, **kw)
        db.session.commit()
        return tr.uuid, tr.id


def test_branch_price_and_hidden_items_only_reach_that_branch(env):
    from tests.test_sync_hub_node import _add_item, _checkout, _item, _register_member, tenant_ctx
    burger = _add_item(env.hub, '分店價漢堡', price=100, stock=50, takeout_price=110)
    salad = _add_item(env.hub, '分店下架沙拉', price=80, stock=50)
    _register_member(env.hub, '分店價會員', '0911000001')
    env.sync_all()
    owner = login_staff(env.hub, 'hq_branch_owner', role='owner')
    page = owner.get('/admin/branch-menu').get_data(as_text=True)
    assert '台南店' in page and 'id="branchMenuForm"' in page
    bid, sid = _item(env.hub, burger)['id'], _item(env.hub, salad)['id']
    r = owner.post('/admin/branch-menu', data={'node': env.node_a_uuid, f'price_{bid}': '120', f'hidden_{sid}': '1'},
                   follow_redirects=True)
    assert '已更新【台南店】2 項' in r.get_data(as_text=True)
    with tenant_ctx(env.hub):
        assert BranchMenuOverride.query.count() == 2
    # 分店不能自己改
    assert login_staff(env.a, 'a_branch_mgr', role='manager').post('/admin/branch-menu', data={}).status_code == 403
    env.sync_all()

    with tenant_ctx(env.b):
        assert BranchMenuOverride.query.count() == 0      # 只送給台南店
    # 台南店：點餐頁顯示分店價、沙拉下架；外帶價依價差同步加 20
    kiosk_a = env.a.test_client()
    kiosk_a.get('/?shop=default')
    kiosk_a.post('/guest_login')
    html = kiosk_a.get('/').get_data(as_text=True)
    assert '分店價漢堡' in html and '$120' in html and '分店下架沙拉' not in html
    a_burger, a_salad = _item(env.a, burger)['id'], _item(env.a, salad)['id']
    order = _checkout(env.a, [line(a_burger, 2)], expected_total=240)
    assert order['total'] == 240
    takeout = _checkout(env.a, [line(a_burger)], order_type='外帶')
    assert takeout['total'] == 130
    with pytest.raises(AppError, match='此分店未販售'):
        _checkout(env.a, [line(a_salad)])
    readonly = login_staff(env.a, 'a_branch_mgr', role='manager').get('/admin/branch-menu').get_data(as_text=True)
    assert 'id="branchMenuReadonly"' in readonly and '本分店下架' in readonly and '$120' in readonly
    # 促銷試算也用分店價 (與結帳一致，否則畫面金額與結帳不同)
    from app.models.promotion import Promotion
    with tenant_ctx(env.a) as tenant:
        db.session.add(Promotion(tenant_id=tenant.id, name='分店價九折', kind='threshold_discount', target_type='items',
                                 target_value=burger, percent=90, threshold=0))
        db.session.commit()
    preview = kiosk_a.post('/api/promotions/preview', json={'items': [line(a_burger)], 'channel': 'kiosk'}).get_json()
    assert preview['discount'] == 12, preview
    with tenant_ctx(env.a):
        Promotion.query.filter_by(name='分店價九折').delete()
        db.session.commit()
    # 對外 API 的菜單也是本分店的價格 / 下架
    from app.models.integration import ApiKey
    with tenant_ctx(env.a) as tenant:
        key = ApiKey(tenant_id=tenant.id, name='分店 API', scopes='menu:read')
        raw = key.issue()
        db.session.add(key)
        db.session.commit()
    menu = {m['name']: m for m in env.a.test_client().get('/api/v1/menu', headers={'Authorization': f'Bearer {raw}'})
            .get_json()['items']}
    assert menu['分店價漢堡']['price'] == 120 and menu['分店價漢堡']['prices']['takeout'] == 130
    assert menu['分店下架沙拉']['branch_hidden'] is True and menu['分店下架沙拉']['is_available'] is False
    # 分店價比總部便宜太多，會讓特價變成 0 元以下：拒絕
    drink = _add_item(env.hub, '分店特價飲', price=60, stock=10, is_discount=True, discount_price=30)
    did = _item(env.hub, drink)['id']
    bad = owner.post('/admin/branch-menu', data={'node': env.node_a_uuid, f'price_{did}': '20'}, follow_redirects=True)
    assert '特價 $30→$-10' in bad.get_data(as_text=True)
    with tenant_ctx(env.hub):
        assert BranchMenuOverride.query.filter_by(menu_item_id=did).count() == 0
    # POS 也套用
    pos = login_staff(env.a, 'a_branch_pos', role='staff').get('/pos').get_data(as_text=True)
    assert '$120' in pos and '分店下架沙拉' not in pos
    # 高雄店：總部價、可點沙拉
    assert _checkout(env.b, [line(_item(env.b, burger)['id'])])['total'] == 100
    assert _checkout(env.b, [line(_item(env.b, salad)['id'])])['total'] == 80
    # 線上點餐：依取餐分店
    assert _checkout(env.hub, [line(bid)], node_uuid=env.node_a_uuid)['total'] == 120
    assert _checkout(env.hub, [line(bid)], node_uuid=env.node_b_uuid)['total'] == 100
    with pytest.raises(AppError, match='此分店未販售'):
        _checkout(env.hub, [line(sid)], node_uuid=env.node_a_uuid)
    # 後台菜單編輯仍顯示總部價
    with tenant_ctx(env.a):
        assert MenuItem.query.filter_by(uuid=burger).one().list_price == 100

    # 恢復總部設定
    r = owner.post('/admin/branch-menu', data={'node': env.node_a_uuid, f'price_{bid}': '', f'price_{sid}': ''},
                   follow_redirects=True)
    assert '已更新【台南店】2 項' in r.get_data(as_text=True)
    env.sync_all()
    assert _checkout(env.a, [line(a_burger)])['total'] == 100
    assert _checkout(env.a, [line(a_salad)])['total'] == 80


def test_request_from_central_kitchen_ship_and_receive_with_difference(env):
    from app.services import monitor_service
    _ing(env.hub, '雞胸肉', stock=100, unit_cost=2, unit='kg')
    _ing(env.a, '雞胸肉', stock=5, unit_cost=3, unit='kg')
    env.sync_all()   # 分店收到分店名單
    from app.services import branch_service
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(env.a) as tenant:
        names = [s['name'] for s in branch_service.sites(tenant.id)]
    assert names[:3] == ['總部 / 中央廚房', '台南店', '高雄店']

    uuid, _aid = _act(env.a, 'create_request', 'HQ', [{'name': '雞胸肉', 'qty': 30}], note='週五前')
    env.sync(env.a)
    hub_tr = _tr(env.hub, uuid)
    assert hub_tr['status'] == 'requested' and hub_tr['lines'][0]['qty'] == 30
    assert _tr(env.b, uuid) is None
    env.sync(env.b)
    assert _tr(env.b, uuid) is None                       # 與高雄店無關，不會下載

    # 總部頁面待處理、同時按兩次出貨只扣一次
    hq = login_staff(env.hub, 'hq_branch_owner', role='owner')
    page = hq.get('/admin/transfers').get_data(as_text=True)
    assert 'id="transferTodo"' in page and '台南店 向本店叫貨' in page

    def ship_once():
        try:
            _act(env.hub, 'ship', hub_tr['id'], {'雞胸肉': 28})
            return 'ok'
        except AppError as exc:
            return exc.status
    results = run_together(ship_once, ship_once)
    assert sorted(results, key=str) == [409, 'ok'], results
    assert _ing(env.hub, '雞胸肉')[0] == 72
    with tenant_ctx(env.hub):
        assert StockMovement.query.filter_by(reason='transfer_out').count() == 1

    alerts = []
    real_alert = monitor_service.alert
    monitor_service.alert = lambda msg, **kw: alerts.append(msg)
    try:
        env.sync(env.a)
        a_tr = _tr(env.a, uuid)
        assert a_tr['status'] == 'shipped' and a_tr['lines'][0]['shipped'] == 28 and a_tr['lines'][0]['unit_cost'] == 2
        mgr = login_staff(env.a, 'a_branch_mgr', role='manager')
        detail = mgr.get(f"/admin/transfers/{a_tr['id']}").get_data(as_text=True)
        assert 'id="transferReceiveBtn"' in detail
        r = mgr.post(f"/admin/transfers/{a_tr['id']}", data={'action': 'receive', 'qty_0': '27'}, follow_redirects=True)
        assert '已收貨入庫' in r.get_data(as_text=True)
    finally:
        monitor_service.alert = real_alert
    stock, cost, _unit = _ing(env.a, '雞胸肉')
    assert stock == 32 and cost == round((5 * 3 + 27 * 2) / 32, 4)
    assert any('收貨數量與出貨不符' in a for a in alerts), alerts
    env.sync(env.a)
    hub_tr = _tr(env.hub, uuid)
    assert hub_tr['status'] == 'received' and '雞胸肉 出 28 收 27' in hub_tr['reason']
    # 已收貨不能再收一次
    with pytest.raises(AppError, match='已收貨'):
        _act(env.a, 'receive', a_tr['id'], {})


def test_branch_to_branch_transfer_creates_ingredient_and_rejects_forged_status(env):
    from app.models.sync import SyncConflict
    from tests.test_sync_hub_node import tenant_ctx
    _ing(env.b, '洋蔥', stock=50, unit_cost=1.5, unit='kg')
    with pytest.raises(AppError, match='不足出貨'):
        _act(env.b, 'create_transfer', env.node_a_uuid, [{'name': '洋蔥', 'qty': 80}])
    assert _ing(env.b, '洋蔥')[0] == 50
    uuid, b_id = _act(env.b, 'create_transfer', env.node_a_uuid, [{'name': '洋蔥', 'qty': 10}])
    assert _ing(env.b, '洋蔥')[0] == 40
    # 出貨方偽造「已收貨」：線上端拒絕 (只有收貨方可以收貨)
    with tenant_ctx(env.b):
        t = db.session.get(StockTransfer, b_id)
        env.sync(env.b)
        t = db.session.get(StockTransfer, b_id)
        t.status = 'received'
        db.session.commit()
    env.sync(env.b)
    with env.b.app_context():
        assert any('不可代替其他分店' in c.reason for c in SyncConflict.query.all())
    assert _tr(env.hub, uuid)['status'] == 'shipped'
    env.sync(env.a)
    a_tr = _tr(env.a, uuid)
    assert a_tr['status'] == 'shipped'
    assert _ing(env.a, '洋蔥') is None
    _act(env.a, 'receive', a_tr['id'], {})
    assert _ing(env.a, '洋蔥') == (10, 1.5, 'kg')        # 自動建立原料，沿用出貨方單位與成本
    env.sync_all()
    assert _tr(env.hub, uuid)['status'] == 'received'


def test_cancelled_request_shipped_before_sync_converges_to_shipped(env):
    from app.services import monitor_service
    _ing(env.b, '鮮奶', stock=20, unit_cost=0.1, unit='ml')
    _ing(env.a, '鮮奶', stock=0, unit_cost=0.1, unit='ml')
    uuid, a_id = _act(env.a, 'create_request', env.node_b_uuid, [{'name': '鮮奶', 'qty': 6}])
    env.sync(env.a)
    env.sync(env.b)
    b_id = _tr(env.b, uuid)['id']
    # 兩邊在同步前各自操作：台南店取消、高雄店出貨
    _act(env.a, 'cancel', a_id, '不需要了')
    _act(env.b, 'ship', b_id, {})
    assert _ing(env.b, '鮮奶')[0] == 14
    alerts = []
    real_alert = monitor_service.alert
    monitor_service.alert = lambda msg, **kw: alerts.append(msg)
    try:
        env.sync(env.a)
        assert _tr(env.hub, uuid)['status'] == 'cancelled'
        env.sync(env.b)
    finally:
        monitor_service.alert = real_alert
    assert _tr(env.hub, uuid)['status'] == 'shipped'
    assert any('已被台南店取消' in a for a in alerts), alerts
    env.sync(env.a)
    assert _tr(env.a, uuid)['status'] == 'shipped'      # 取消被出貨覆蓋，收貨方可以收貨
    _act(env.a, 'receive', a_id, {})
    assert _ing(env.a, '鮮奶')[0] == 6
    # 只有出貨方可以拒絕 / 只有收貨方可以取消
    uuid2, a2 = _act(env.a, 'create_request', env.node_b_uuid, [{'name': '鮮奶', 'qty': 1}])
    with pytest.raises(AppError, match='只有出貨方可以拒絕'):
        _act(env.a, 'reject', a2, '')
    env.sync(env.a)
    env.sync(env.b)
    with pytest.raises(AppError, match='只有叫貨的分店可以取消'):
        _act(env.b, 'cancel', _tr(env.b, uuid2)['id'])
    _act(env.b, 'reject', _tr(env.b, uuid2)['id'], '缺貨')
    env.sync_all()
    assert _tr(env.a, uuid2)['status'] == 'rejected' and _tr(env.a, uuid2)['reason'] == '缺貨'


def test_cancel_after_shipment_is_reported_to_both_sides(env, monkeypatch):
    from app.services import monitor_service
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append(msg))
    _ing(env.hub, '砂糖', stock=50, unit_cost=0.05, unit='g')
    _ing(env.a, '砂糖', stock=0, unit_cost=0.05, unit='g')
    uuid, a_id = _act(env.a, 'create_request', 'HQ', [{'name': '砂糖', 'qty': 5}])
    env.sync(env.a)
    _act(env.hub, 'ship', _tr(env.hub, uuid)['id'], {})          # 總部先出貨
    _act(env.a, 'cancel', a_id, '不需要了')                         # 台南店同步前取消
    env.sync(env.a)   # 上傳取消 (線上端以已出貨為準並通知)，再下載已出貨 (台南店也通知)
    assert _tr(env.hub, uuid)['status'] == 'shipped'
    assert any('取消未生效' in a for a in alerts), alerts             # 線上端
    assert _tr(env.a, uuid)['status'] == 'shipped'
    assert any('已被台南店取消' in a for a in alerts), alerts          # 台南店下載時
    _act(env.a, 'receive', a_id, {})
    assert _ing(env.a, '砂糖')[0] == 5
    env.sync_all()


def test_invariants_after_branch_features(env):
    import sqlalchemy as sa

    from app.sync.registry import SPECS
    for app_ in (env.hub, env.a, env.b):
        with app_.app_context():
            for spec in SPECS:   # 沒有留下未提交的同步標記
                t = spec.model.__table__
                assert db.session.execute(sa.select(sa.func.count()).select_from(t).where(t.c.sync_rev < 0)).scalar() == 0
    # 三邊的叫貨 / 調撥單狀態一致 (與自己相關的單據)
    with env.hub.app_context():
        hub = {t.uuid: t.status for t in StockTransfer.query.execution_options(skip_tenant_filter=True).all()}
    for app_, uuid in ((env.a, env.node_a_uuid), (env.b, env.node_b_uuid)):
        with app_.app_context():
            mine = {t.uuid: t.status for t in StockTransfer.query.execution_options(skip_tenant_filter=True).all()}
        assert mine == {u: s for u, s in hub.items() if u in mine}


def test_hub_does_not_take_reservations(env):
    """訂位 / 訂金是門市本機資料：線上端不提供 (否則收到的訂位與訂金沒有分店看得到)"""
    c = env.hub.test_client()
    for path in ('/reserve', '/waitlist', '/reserve/abc/pay'):
        assert c.get(path).status_code == 404, path
    assert c.post('/payment/ecpay/reservation_notify', data={}).status_code == 404
