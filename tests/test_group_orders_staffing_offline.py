"""第四輪：團體預購、排班建議、點餐機斷線暫存補送"""
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.group_order import GroupOrder, GroupOrderEntry
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.staff import WorkSchedule
from app.models.tenant import StaffAccount, Tenant
from app.models.user import StaffAuditLog
from app.services import group_order_service, monitor_service, settings_service, staffing_service
from tests.support import assert_flash, guest_client, login_staff, make_member, member_client, run_together


def _tenant(app, slug):
    with app.app_context():
        t = Tenant.query.filter_by(slug=slug).first()
        if t is None:
            t = Tenant(name=f'測試 {slug}', slug=slug)
            db.session.add(t)
            db.session.commit()
        return t.id


@contextmanager
def _ctx(app, tenant_id):
    from app.tenant_context import _use_tenant
    with app.test_request_context('/'):
        _use_tenant(db.session.get(Tenant, tenant_id), remember=False)
        yield


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


def _enable_groups(app, tid, **over):
    with _ctx(app, tid):
        cfg = dict(settings_service.get('group_order', tid), enabled=True, min_lead_minutes=60, **over)
        settings_service.put('group_order', cfg, tenant_id=tid)
        db.session.commit()


def _items(app, tid, *specs):
    with _ctx(app, tid):
        ids = []
        for name, price, kw in specs:
            m = MenuItem(tenant_id=tid, name=name, price=price, stock=kw.pop('stock', 99), total_stock=99, category='便當', **kw)
            db.session.add(m)
            db.session.commit()
            ids.append(m.id)
        return ids


def _slot(hours=3):
    t = datetime.now() + timedelta(hours=hours)
    return t.replace(minute=0, second=0, microsecond=0)


# ===========================================================================
# 團體預購
# ===========================================================================
def test_group_order_disabled_returns_404(app):
    tid = _tenant(app, 'grp-off')
    c = app.test_client()
    assert c.get('/shop/grp-off/group/new').status_code == 404
    assert c.get('/shop/grp-off/group/whatever').status_code == 404
    with _ctx(app, tid):
        assert settings_service.get('group_order', tid).get('enabled') is False


def test_group_order_create_join_edit_manage_and_submit(app):
    tid = _tenant(app, 'grp-shop')
    _enable_groups(app, tid, max_qty_per_person=5, max_people=3)
    chicken, tea, gone = _items(app, tid, ('團購雞腿飯', 120, {'takeout_price': 125}),
                                ('團購紅茶', 30, {'size_options': '中杯:0,大杯:+10'}),
                                ('團購售完品', 80, {'is_sold_out': True}))
    pickup = _slot(4)
    organizer = app.test_client()
    page = organizer.get('/shop/grp-shop/group/new')
    assert page.status_code == 200 and '團購雞腿飯' not in page.get_data(as_text=True)
    form = dict(title='業務部午餐', organizer_name='王主揪', organizer_phone='0912-345-678',
                pickup_at=pickup.strftime('%Y-%m-%dT%H:%M'), deadline='', order_type='外帶', auto_submit='1',
                note='預算 150 內')
    for bad, msg in ((dict(organizer_phone='12345'), '手機號碼'), (dict(title=''), '團購名稱'),
                     (dict(pickup_at=(datetime.now() + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M')), '分鐘後'),
                     (dict(pickup_at=(datetime.now() + timedelta(days=40)).strftime('%Y-%m-%dT%H:%M')), '天內'),
                     (dict(deadline=(pickup - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M')), '15 分鐘'),
                     (dict(deadline=(datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')), '晚於現在')):
        organizer.post('/shop/grp-shop/group/new', data=dict(form, **bad))
        assert_flash(organizer, msg)
    created = organizer.post('/shop/grp-shop/group/new', data=form)
    html = created.get_data(as_text=True)
    assert created.status_code == 200 and '只顯示這一次' in html and created.headers['Cache-Control'] == 'no-store'
    manage_url = re.search(r'id="groupManageUrl">([^<]+)<', html).group(1).replace('&amp;', '&')
    with app.app_context():
        grp = GroupOrder.query.filter_by(tenant_id=tid, title='業務部午餐').one()
        token, gid = grp.token, grp.id
        assert grp.organizer_phone == '0912345678' and grp.deadline == pickup - timedelta(minutes=60)
        assert grp.auto_submit is True and grp.manage_key_hash not in manage_url
    share = f'/shop/grp-shop/group/{token}'

    # 參加者：選餐、驗證、修改、刪除
    amy, bob, carl = app.test_client(), app.test_client(), app.test_client()
    join_page = amy.get(share).get_data(as_text=True)
    assert '團購雞腿飯' in join_page and '$125' in join_page and '團購售完品' not in join_page and '預算 150 內' in join_page
    for bad, msg in (({}, '至少選一份'), ({f'qty_{chicken}': '6'}, '每人最多 5 份'), ({f'qty_{gone}': '1'}, '無法供應'),
                     ({f'qty_{tea}': '1', f'size_{tea}': '特大杯'}, '規格不正確'), ({f'qty_{chicken}': '1', 'name': ' '}, '名字')):
        amy.post(share, data=dict({'action': 'add', 'name': 'Amy'}, **bad))
        assert_flash(amy, msg)
    amy.post(share, data={'action': 'add', 'name': 'Amy', f'qty_{chicken}': '1', f'qty_{tea}': '2', f'size_{tea}': '大杯',
                          f'note_{tea}': '少冰'})
    bob.post(share, data={'action': 'add', 'name': 'Bob', f'qty_{chicken}': '2'})
    carl.post(share, data={'action': 'add', 'name': 'Carl', f'qty_{tea}': '1'})
    assert amy.get(share).get_data(as_text=True).count('我的點餐：') == 1
    with amy.session_transaction() as sess:
        amy_key = sess['group_entries'][token][0]
    # 人數上限 3
    app.test_client().post(share, data={'action': 'add', 'name': 'Dan', f'qty_{tea}': '1'})
    with app.app_context():
        assert GroupOrderEntry.query.filter_by(group_order_id=gid).count() == 3
    # 別人的 entry_key：查無
    assert bob.post(share, data={'action': 'delete', 'entry_key': amy_key}).status_code == 404
    # 個人修改連結 (換手機)
    phone2 = app.test_client()
    assert phone2.get(f'{share}?entry={amy_key}').status_code == 302
    assert '我的點餐：Amy' in phone2.get(share).get_data(as_text=True)
    phone2.post(share, data={'action': 'update', 'entry_key': amy_key, 'name': 'Amy 王', f'qty_{chicken}': '1',
                             f'qty_{tea}': '2', f'size_{tea}': '大杯', f'note_{tea}': '少冰'})
    with carl.session_transaction() as sess:
        carl_key = sess['group_entries'][token][0]
    carl.post(share, data={'action': 'delete', 'entry_key': carl_key})
    with app.app_context():
        names = [e.name for e in GroupOrderEntry.query.filter_by(group_order_id=gid).order_by(GroupOrderEntry.id)]
        assert names == ['Amy 王', 'Bob']

    # 主揪管理：管理金鑰錯誤 / 其他人不能進入
    assert app.test_client().get(f'{share}/manage?k=wrong').status_code == 404
    assert bob.get(f'{share}/manage').status_code == 404
    assert bob.get(f'{share}/print').status_code == 404
    other_browser = app.test_client()
    parts = urlsplit(manage_url)
    assert other_browser.get(f'{parts.path}?{parts.query}').status_code == 302   # 管理連結：記住後導回乾淨網址
    manage = other_browser.get(f'{share}/manage').get_data(as_text=True)
    # 合計：雞腿 3 份 (外帶 125)、大杯紅茶 2 份 (30 + 10)
    assert 'Amy 王' in manage and 'Bob' in manage and '$455' in manage and '共 5 份' in manage
    assert 'Amy 王' in organizer.get(f'{share}/print').get_data(as_text=True)

    r = organizer.post(f'{share}/manage', data={'action': 'submit'}, follow_redirects=True)
    assert '已送單，取餐號' in r.get_data(as_text=True)
    with app.app_context():
        grp = db.session.get(GroupOrder, gid)
        order = grp.order
        assert grp.status == 'submitted' and order.uuid == grp.uuid and order.pickup_at == pickup
        assert order.order_type == '外帶' and order.total_price == 455 and order.payment_status in ('', None)
        lines = sorted((i.item_name, i.quantity, i.customization or '') for i in order.items)
        assert lines[0][:2] == ('團購紅茶', 2) and '大杯' in lines[0][2] and '少冰' in lines[0][2]
        assert lines[1] == ('團購雞腿飯', 3, '')
        assert order.note.startswith('團購「業務部午餐」王主揪 0912345678｜') and 'Amy 王:' in order.note
        assert order.external_ref == f'group:{token}' and order.table_number == '團購 王主揪'
    # 再送一次 / 送單後加點都不行
    organizer.post(f'{share}/manage', data={'action': 'submit'})
    assert_flash(organizer, '已送單或已取消')
    amy.post(share, data={'action': 'add', 'name': 'Late', f'qty_{chicken}': '1'})
    assert_flash(amy, '已截止')
    assert '已送單' in amy.get(share).get_data(as_text=True)


def test_group_order_auto_submit_failure_cancel_and_race(app, alerts):
    tid = _tenant(app, 'grp-auto')
    _enable_groups(app, tid)
    rice, scarce = _items(app, tid, ('自動送單飯', 100, {}), ('限量湯', 50, {'stock': 1}))
    now = datetime.now()
    pickup = _slot(5)

    def make(title, lines, auto=True):
        with _ctx(app, tid):
            grp, _key = group_order_service.create(tid, {'title': title, 'organizer_name': '主揪', 'organizer_phone': '0911000111',
                                                          'pickup_at': pickup.strftime('%Y-%m-%dT%H:%M'),
                                                          'auto_submit': '1' if auto else '0'}, now=now)
            db.session.flush()
            for name, item_lines in lines:
                group_order_service.save_entry(grp, name, item_lines, now=now)
            db.session.commit()
            return grp.id

    ok = make('自動送單', [('甲', [{'menu_item_id': rice, 'quantity': 2}]), ('乙', [{'menu_item_id': rice, 'quantity': 1}])])
    fail = make('會失敗', [('丙', [{'menu_item_id': scarce, 'quantity': 3}])])
    manual = make('手動送單', [('丁', [{'menu_item_id': rice, 'quantity': 1}])], auto=False)
    empty = make('沒人點', [])
    deadline = pickup - timedelta(minutes=60)
    with _ctx(app, tid):
        assert group_order_service.run_due(tid, now=deadline - timedelta(minutes=1)) == (0, 0)
        sent, cancelled = group_order_service.run_due(tid, now=deadline + timedelta(minutes=1))
        assert (sent, cancelled) == (3, 0)
        g_ok, g_fail, g_manual, g_empty = (db.session.get(GroupOrder, i) for i in (ok, fail, manual, empty))
        assert g_ok.status == 'submitted' and sum(i.quantity for i in g_ok.order.items) == 3
        assert g_fail.status == 'failed' and '限量湯' in g_fail.error and g_fail.order_id is None
        assert g_empty.status == 'failed' and g_empty.error == '沒有人點餐'
        assert g_manual.status == 'open'
        assert any('會失敗' in a for a in alerts)
        # 取餐時間過了仍未送的手動團購：取消
        assert group_order_service.run_due(tid, now=pickup + timedelta(minutes=1)) == (0, 1)
        assert db.session.get(GroupOrder, manual).status == 'cancelled'
        # 失敗後調整再送 (庫存補足)
        db.session.get(MenuItem, scarce).stock = 10
        db.session.commit()
        grp, err = group_order_service.submit(fail, now=deadline + timedelta(minutes=5))
        assert err == '' and grp.status == 'submitted'
        # 取消：已送單的不能取消
        with pytest.raises(AppError):
            group_order_service.cancel(db.session.get(GroupOrder, ok))

    # 送單途中發生非預期錯誤：標示失敗 (不會卡在「送單中」)，之後可再送
    from app.services import order_service
    boom = make('會出錯', [('己', [{'menu_item_id': rice, 'quantity': 1}])])
    real = order_service.process_checkout

    def broken(**kw):
        raise RuntimeError('資料庫連線中斷')
    order_service.process_checkout = broken
    try:
        with _ctx(app, tid):
            grp, err = group_order_service.submit(boom)
            assert err == '送單時發生錯誤，請稍後再試' and grp.status == 'failed' and '資料庫連線中斷' in grp.error
    finally:
        order_service.process_checkout = real
    with _ctx(app, tid):
        assert group_order_service.submit(boom)[1] == ''
        # 行程在送單中途被中斷 (停在 submitting)：排程 5 分鐘後改為失敗
        stuck = make('卡住', [('庚', [{'menu_item_id': rice, 'quantity': 1}])])
        db.session.get(GroupOrder, stuck).status = 'submitting'
        db.session.commit()
        group_order_service.run_due(tid, now=datetime.now() + timedelta(minutes=6))
        assert db.session.get(GroupOrder, stuck).status == 'failed'
        # 已送單 / 送單中的團購：參加者不能再加點 (送單讀取點餐內容後才寫入的會遺失)
        g_stuck = db.session.get(GroupOrder, stuck)
        g_stuck.status = 'submitting'
        db.session.commit()
        from sqlalchemy.orm.attributes import set_committed_value
        set_committed_value(g_stuck, 'status', 'open')   # 模擬參加者讀到的是送單前的狀態
        with pytest.raises(AppError):
            group_order_service.save_entry(g_stuck, '辛', [{'menu_item_id': rice, 'quantity': 1}], now=now)
        db.session.rollback()
        assert GroupOrderEntry.query.filter_by(group_order_id=stuck, name='辛').count() == 0

    # 兩個人同時按送單：只建立一張訂單
    race = make('同時送單', [('戊', [{'menu_item_id': rice, 'quantity': 1}])])

    def go():
        with _ctx(app, tid):
            return group_order_service.submit(race)[1]
    results = run_together(go, go)
    assert sorted(results) == ['', '這個團購已送單或已取消']
    with _ctx(app, tid):
        grp = db.session.get(GroupOrder, race)
        assert Order.query.filter_by(uuid=grp.uuid).count() == 1 and grp.status == 'submitted'


def test_group_order_admin_page_staff_create_and_actions(app):
    tid = _tenant(app, 'grp-admin')
    staff = login_staff(app, 'grp_staff', role='staff', tenant_id=tid)
    assert '尚未開放團體預購' in staff.get('/admin/group-orders').get_data(as_text=True)
    _enable_groups(app, tid)
    (dish,) = _items(app, tid, ('代建便當', 90, {}))
    pickup = _slot(3)
    r = staff.post('/admin/group-orders', data={'action': 'create', 'title': '電話團購', 'organizer_name': '林小姐',
                                                'organizer_phone': '0922333444', 'pickup_at': pickup.strftime('%Y-%m-%dT%H:%M'),
                                                'order_type': '內用'}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert '已建立團購「電話團購」' in html and 'groupCreatedAlert' in html
    with app.app_context():
        grp = GroupOrder.query.filter_by(tenant_id=tid, title='電話團購').one()
        gid, token = grp.id, grp.token
        assert grp.created_by == 'grp_staff' and grp.order_type == '內用'
        assert StaffAuditLog.query.filter(StaffAuditLog.details.contains('電話團購')).count() == 1
    # 店員登入時可進管理頁、列印分裝單
    assert staff.get(f'/shop/grp-admin/group/{token}/manage').status_code == 200
    app.test_client().post(f'/shop/grp-admin/group/{token}', data={'action': 'add', 'name': '客人A', f'qty_{dish}': '2'})
    staff.post('/admin/group-orders', data={'action': 'submit', 'group_id': str(gid)})
    with app.app_context():
        grp = db.session.get(GroupOrder, gid)
        assert grp.status == 'submitted' and grp.order.order_type == '內用' and grp.order.cashier_username == 'grp_staff'
    staff.post('/admin/group-orders', data={'action': 'cancel', 'group_id': str(gid)})
    assert_flash(staff, '不能取消')
    # 其他店家的團購
    other = login_staff(app, 'grp_other_owner', role='owner', tenant_id=_tenant(app, 'grp-other'))
    assert other.post('/admin/group-orders', data={'action': 'cancel', 'group_id': str(gid)}).status_code == 404
    kitchen = login_staff(app, 'grp_kitchen', role='kitchen', tenant_id=tid)
    assert kitchen.get('/admin/group-orders').status_code in (302, 403)


# ===========================================================================
# 排班建議
# ===========================================================================
def test_suggest_shifts_greedy():
    assert staffing_service.suggest_shifts({}, 4) == []
    assert staffing_service.suggest_shifts({12: 2, 13: 0, 14: 0, 15: 0, 16: 1, 17: 1, 18: 1}, 4) == \
        [(12, 16), (12, 16), (15, 19)]
    assert staffing_service.suggest_shifts({10: 1, 11: 1}, 4) == [(10, 12)]
    assert staffing_service._hours_of('09:00', '13:30') == [9, 10, 11, 12, 13]
    assert staffing_service._hours_of('22:00', '02:00') == [22, 23]
    assert staffing_service._hours_of('bad', '10:00') == []


def test_staffing_plan_page_and_apply(app):
    from app.services.order_service import process_checkout
    tid = _tenant(app, 'staff-shop')
    (item,) = _items(app, tid, ('排班測試餐', 100, {'stock': 9999}))
    target = date.today() + timedelta(days=1)
    with _ctx(app, tid):
        for back in (6, 13):   # 過去兩個同一個星期幾：12 點 30 張、18 點 6 張
            day = target - timedelta(days=back + 1)
            for i in range(30):
                process_checkout(tenant_id=tid, items=[{'menu_item_id': item, 'quantity': 1}],
                                 now=datetime.combine(day, datetime.min.time()) + timedelta(hours=12, minutes=i))
            for i in range(6):
                process_checkout(tenant_id=tid, items=[{'menu_item_id': item, 'quantity': 1}],
                                 now=datetime.combine(day, datetime.min.time()) + timedelta(hours=18, minutes=i))
        a = StaffAccount(tenant_id=tid, username='shift_a', role='staff', hourly_wage=200, is_active=True)
        b = StaffAccount(tenant_id=tid, username='shift_b', role='staff', hourly_wage=200, is_active=True)
        a.set_password('x' * 12)
        b.set_password('x' * 12)
        db.session.add_all([a, b])
        db.session.flush()
        db.session.add(WorkSchedule(tenant_id=tid, staff_account_id=a.id, work_date=target, start_time='12:00',
                                    end_time='16:00'))
        db.session.commit()
        a_id, b_id = a.id, b.id
        p = staffing_service.plan(tid, target)
        assert p['days_used'] == 2 and p['revenue'] == 3600
        rows = {r['hour']: r for r in p['rows']}
        assert (rows[12]['orders'], rows[12]['need'], rows[12]['have'], rows[12]['gap']) == (30, 3, 1, -2)
        assert rows[14]['need'] == 1 and rows[14]['gap'] == 0 and rows[18]['need'] == 1 and rows[18]['have'] == 0
        assert p['need_hours'] == 9 and p['have_hours'] == 4 and p['short_hours'] == 5
        assert [(s['start'], s['end']) for s in p['shifts']] == [('12:00', '16:00'), ('12:00', '16:00'), ('15:00', '19:00')]
        assert p['avg_wage'] == 200 and p['labor'] == 1800 and p['labor_ratio'] == 50.0

    owner = login_staff(app, 'staff_owner', role='owner', tenant_id=tid)
    page = owner.get(f'/admin/staffing?date={target}').get_data(as_text=True)
    assert '建議補排的班別' in page and 'shift_b' in page and '50.0%' in page
    owner.post('/admin/staffing', data={'date': target.isoformat(), 'count': '3'})
    assert_flash(owner, '請為建議的班別選擇員工')
    owner.post('/admin/staffing', data={'date': target.isoformat(), 'count': '1', 'staff_0': str(b_id),
                                        'start_0': '25:00', 'end_0': '16:00'})
    assert_flash(owner, 'HH:MM')
    owner.post('/admin/staffing', data={'date': target.isoformat(), 'count': '3', 'staff_0': str(b_id),
                                        'start_0': '12:00', 'end_0': '16:00', 'staff_1': '', 'staff_2': str(a_id),
                                        'start_2': '15:00', 'end_2': '19:00'})
    assert_flash(owner, '已加入 2 筆班表')
    with _ctx(app, tid):
        assert WorkSchedule.query.filter_by(work_date=target).count() == 3
        assert staffing_service.plan(tid, target)['short_hours'] == 1   # 12 點仍缺 1 人
    owner.get('/admin/staffing?date=2026-99-01')
    assert_flash(owner, '日期格式不正確')
    empty = owner.get(f'/admin/staffing?date={target + timedelta(days=3)}').get_data(as_text=True)
    assert '無法估算' in empty
    assert login_staff(app, 'staff_cashier', role='staff', tenant_id=tid).get('/admin/staffing').status_code in (302, 403)


# ===========================================================================
# 點餐機斷線暫存補送
# ===========================================================================
def _offline_body(item_id, **over):
    order = {'client_order_id': str(uuid.uuid4()), 'items': [{'menu_item_id': item_id, 'quantity': 2}],
             'payment_method': 'Credit Card', 'order_type': '外帶', 'note': '少冰', 'use_points': 50,
             'promo_code': 'OPEN20', 'expected_total': 1}
    body = {'shop': 'default', 'code': 'AB12', 'queued_at': (datetime.now() - timedelta(minutes=5)).isoformat(timespec='seconds'),
            'order': order}
    body.update(over)
    return body


def test_kiosk_offline_order_replay(app):
    from tests.support import make_item
    item = make_item(app, '斷線暫存餐', price=80, stock=50)
    reward = make_item(app, '斷線紅利餐', price=50, stock=50, is_reward=True, reward_points=10)
    make_member(app, '斷線會員', '0955111222', points=100, pin='1357')
    member = member_client(app, '0955111222', '1357', ip='10.99.7.1')
    body = _offline_body(item)
    r = member.post('/api/kiosk/offline_order', json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['replayed'] is False and data['code'] == 'AB12' and data['pickup_number'] > 0
    # 只回傳點餐機需要的欄位 (不含會員、付款、發票)
    assert set(data) == {'success', 'replayed', 'code', 'order_id', 'pickup_number', 'total_price'}
    with app.app_context():
        order = Order.query.filter_by(uuid=body['order']['client_order_id']).one()
        # 一律訪客、現金、不用點數 / 優惠券；金額以補送當下為準
        assert order.user_id is None and order.payment_method == 'Cash' and order.total_price == 160
        assert order.note.startswith('[斷線暫存 AB12 ') and order.note.endswith('少冰') and order.table_number == '暫存單 AB12'
        assert StaffAuditLog.query.filter_by(action_type='OFFLINE_ORDER', target_order_id=order.id).count() == 1
        oid = order.id
    # 重送 (例如斷線前其實已送達)：回傳原訂單
    again = guest_client(app).post('/api/kiosk/offline_order', json=body).get_json()
    assert again['replayed'] is True and again['order_id'] == oid
    g = guest_client(app)
    assert g.post('/api/kiosk/offline_order', json=_offline_body(item, shop='other-shop')).status_code == 409
    expired = g.post('/api/kiosk/offline_order', json=_offline_body(
        item, queued_at=(datetime.now() - timedelta(hours=4)).isoformat()))
    assert expired.status_code == 410 and expired.get_json()['expired'] is True
    bad = _offline_body(item)
    bad['order']['items'] = [{'menu_item_id': reward, 'quantity': 1, 'is_reward': True}]
    assert g.post('/api/kiosk/offline_order', json=bad).status_code == 409
    no_id = _offline_body(item)
    no_id['order']['client_order_id'] = ''
    assert g.post('/api/kiosk/offline_order', json=no_id).status_code == 400
    assert g.post('/api/kiosk/offline_order', json=_offline_body(item, queued_at='yesterday')).status_code == 400
    # 時區格式 (UTC) 也接受
    utc = _offline_body(item, queued_at=(datetime.now().astimezone() - timedelta(minutes=1)).isoformat())
    assert g.post('/api/kiosk/offline_order', json=utc).status_code == 200
    # 會員訂單的編號 (追蹤頁網址上看得到)：不可藉暫存單補送查到別人的收據
    member_order = member.post('/submit_order', json={'client_order_id': str(uuid.uuid4()),
                                                      'items': [{'menu_item_id': item, 'quantity': 1}]}).get_json()
    with app.app_context():
        member_uuid = db.session.get(Order, member_order['order_id']).uuid
    peek = _offline_body(item)
    peek['order']['client_order_id'] = member_uuid
    leaked = g.post('/api/kiosk/offline_order', json=peek)
    assert leaked.status_code == 409 and '斷線會員' not in leaked.get_data(as_text=True)
    # 不是本機的訂單編號 (例如 POS 單) 不回傳內容
    with app.app_context():
        pos_order = Order.query.filter(Order.order_source == 'POS').first()
    if pos_order is not None:
        clash = _offline_body(item)
        clash['order']['client_order_id'] = pos_order.uuid
        assert g.post('/api/kiosk/offline_order', json=clash).status_code == 409
