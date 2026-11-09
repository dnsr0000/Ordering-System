"""修正回歸 (group-orders-2)：團購頁套用分店價格 / 下架、連點兩下不重複建立、功能關閉後店員仍可管理、
預填取餐時間保留填表時間、員工開別家團購連結、記住的個人金鑰被擠掉時提示、修改時被主揪移除不 500、
管理頁狀態 / 用餐方式翻譯、送單後訂單被取消、英文截止提示空格"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.group_order import GroupOrder, GroupOrderEntry
from app.models.menu import MenuItem
from app.services import group_order_service, settings_service
from tests.support import assert_flash, flashes, get_or_create_tenant, login_staff, tenant_request


def _shop(app, slug, languages=None, **over):
    tid = get_or_create_tenant(app, slug)
    with tenant_request(app, tid):
        cfg = dict(settings_service.get('group_order', tid), enabled=True, min_lead_minutes=60, **over)
        settings_service.put('group_order', cfg, tenant_id=tid)
        if languages:
            settings_service.put('menu', dict(settings_service.get('menu', tid), languages=languages), tenant_id=tid)
        db.session.commit()
    return tid


def _item(app, tid, name, price=100, **kw):
    with tenant_request(app, tid):
        m = MenuItem(tenant_id=tid, name=name, price=price, stock=99, total_stock=99, category='便當', **kw)
        db.session.add(m)
        db.session.commit()
        return m.id


def _group(app, tid, entries=(), **form):
    pickup = (datetime.now() + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
    with tenant_request(app, tid):
        grp, key = group_order_service.create(tid, dict({'title': '修測團購', 'organizer_name': '主揪',
                                                          'organizer_phone': '0911000111',
                                                          'pickup_at': pickup.strftime('%Y-%m-%dT%H:%M')}, **form))
        db.session.flush()
        for name, lines in entries:
            group_order_service.save_entry(grp, name, lines)
        db.session.commit()
        return grp.id, grp.token, key


def _manager(app, slug, token, key):
    c = app.test_client()
    assert c.get(f'/shop/{slug}/group/{token}/manage?k={key}').status_code == 302
    return c


def test_join_page_uses_branch_price_and_hidden_items(app, monkeypatch):
    """B-442：分店主機的團購頁與送單一樣套用分店價 / 分店下架"""
    from app.models.branch import BranchMenuOverride
    from app.sync import runtime
    tid = _shop(app, 'fix-go2-branch')
    priced = _item(app, tid, '修測分店價便當', 100)
    hidden = _item(app, tid, '修測分店下架麵', 80)
    with app.app_context():
        db.session.add_all([BranchMenuOverride(tenant_id=tid, node_uuid='fix-go2-node', menu_item_id=priced, price=150),
                            BranchMenuOverride(tenant_id=tid, node_uuid='fix-go2-node', menu_item_id=hidden, is_hidden=True)])
        db.session.commit()
    monkeypatch.setattr(runtime, 'is_hub', lambda: False)
    monkeypatch.setattr(runtime, 'is_node', lambda: True)
    monkeypatch.setattr(runtime, 'local_node_uuid', lambda: 'fix-go2-node')
    _gid, token, _key = _group(app, tid)
    share = f'/shop/fix-go2-branch/group/{token}'
    c = app.test_client()
    html = c.get(share).get_data(as_text=True)
    assert '修測分店價便當' in html and '$150' in html and '修測分店下架麵' not in html
    c.post(share, data={'action': 'add', 'name': 'A', f'qty_{hidden}': '1'})
    assert_flash(c, '無法供應')
    c.post(share, data={'action': 'add', 'name': 'A', f'qty_{priced}': '2'})
    assert '$300' in c.get(share).get_data(as_text=True)


def test_double_submit_with_same_form_creates_one_entry(app):
    """B-446：同一張表單 (同一個 nonce) 送出兩次只建立一筆，且金鑰記在瀏覽器"""
    tid = _shop(app, 'fix-go2-dup')
    rice = _item(app, tid, '修測重送飯')
    gid, token, _key = _group(app, tid)
    share = f'/shop/fix-go2-dup/group/{token}'
    c = app.test_client()
    nonce = 'fixgo2nonce-AAAAAAAAAAAAAAAA'
    form = {'action': 'add', 'name': 'Amy', f'qty_{rice}': '1', 'entry_nonce': nonce}
    c.post(share, data=form)
    other = app.test_client()   # 另一個請求帶著舊 Cookie (Set-Cookie 沒套用)
    other.post(share, data=form)
    with app.app_context():
        assert GroupOrderEntry.query.filter_by(group_order_id=gid).count() == 1
    for client in (c, other):
        with client.session_transaction() as sess:
            assert sess['group_entries'][token] == [nonce]
        assert '我的點餐：Amy' in client.get(share).get_data(as_text=True)
    # 返回上一頁 (同一個 nonce) 替另一位同事點不同內容：照常建立新的一筆
    c.post(share, data=dict(form, name='Bob'))
    with app.app_context():
        assert GroupOrderEntry.query.filter_by(group_order_id=gid).count() == 2
    assert 'entry_nonce' in c.get(share).get_data(as_text=True)


def test_staff_can_manage_and_print_after_feature_disabled(app):
    """R2-170：關閉團體預購後，店員仍可開啟既有團購的管理頁與分裝單；顧客仍為 404"""
    tid = _shop(app, 'fix-go2-off')
    rice = _item(app, tid, '修測關閉飯')
    _gid, token, _key = _group(app, tid, entries=[('甲', [{'menu_item_id': rice, 'quantity': 1}])])
    with tenant_request(app, tid):
        settings_service.put('group_order', dict(settings_service.get('group_order', tid), enabled=False), tenant_id=tid)
        db.session.commit()
    staff = login_staff(app, 'fix_go2_off_staff', 'staff', tenant_id=tid)
    base = f'/shop/fix-go2-off/group/{token}'
    assert staff.get(f'{base}/manage').status_code == 200
    assert '甲' in staff.get(f'{base}/print').get_data(as_text=True)
    guest = app.test_client()
    assert guest.get(base).status_code == 404
    assert guest.get(f'{base}/manage').status_code == 404


def test_default_pickup_leaves_at_least_30_minutes(monkeypatch):
    """R2-171：預填的取餐時間往上取整，至少比最短提前時間多 30 分鐘"""
    from app.routes import group_orders as routes

    for minute in (0, 1, 29, 30, 31, 59):
        fixed = datetime(2026, 11, 10, 10, minute, 30)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(routes, 'datetime', FakeDT)
        slot = routes._default_pickup({'min_lead_minutes': 120})
        assert slot.minute in (0, 30) and slot.second == 0
        assert timedelta(minutes=150) <= slot - fixed < timedelta(minutes=180), (minute, slot)


def test_staff_of_other_shop_cannot_use_group_links(app):
    """R2-172：已登入員工開別家店的團購網址時明確拒絕，不可建到自己的店"""
    tid = _shop(app, 'fix-go2-shop-b')
    other = _shop(app, 'fix-go2-shop-a')
    _gid, token, _key = _group(app, tid)
    staff = login_staff(app, 'fix_go2_shop_a_owner', 'owner', tenant_id=other)
    r = staff.get('/shop/fix-go2-shop-b/group/new')
    assert r.status_code == 403 and '員工身分登入' in r.get_data(as_text=True)
    pickup = (datetime.now() + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
    r = staff.post('/shop/fix-go2-shop-b/group/new', data={'title': '跨店團', 'organizer_name': 'X',
                                                           'organizer_phone': '0911000111',
                                                           'pickup_at': pickup.strftime('%Y-%m-%dT%H:%M')})
    assert r.status_code == 403
    with app.app_context():
        assert GroupOrder.query.filter_by(title='跨店團').execution_options(skip_tenant_filter=True).count() == 0
    assert staff.get(f'/shop/fix-go2-shop-b/group/{token}').status_code == 403
    assert staff.get('/shop/fix-go2-shop-a/group/new').status_code == 200   # 自己的店照常


def test_dropped_entry_key_is_announced_with_personal_link(app):
    """R2-173：同一瀏覽器第 11 筆起，被擠掉的點餐會提示個人修改連結"""
    tid = _shop(app, 'fix-go2-many', max_people=30, max_qty_per_person=5)
    rice = _item(app, tid, '修測多筆飯')
    _gid, token, _key = _group(app, tid)
    share = f'/shop/fix-go2-many/group/{token}'
    c = app.test_client()
    for i in range(10):
        c.post(share, data={'action': 'add', 'name': f'同事{i}', f'qty_{rice}': '1'})
        flashes(c)
    c.post(share, data={'action': 'add', 'name': '同事10', f'qty_{rice}': '1'})
    msgs = ' '.join(flashes(c))
    assert '同事0' in msgs and '個人修改連結' in msgs and f'/group/{token}?entry=' in msgs


def test_update_after_organizer_removed_entry_is_404_not_500(app, monkeypatch):
    """R2-174：修改點餐時主揪剛好移除這筆：回「找不到」而不是 500"""
    from sqlalchemy import delete
    tid = _shop(app, 'fix-go2-stale')
    rice = _item(app, tid, '修測移除飯')
    gid, token, _key = _group(app, tid)
    share = f'/shop/fix-go2-stale/group/{token}'
    c = app.test_client()
    c.post(share, data={'action': 'add', 'name': 'Amy', f'qty_{rice}': '1'})
    with c.session_transaction() as sess:
        key = sess['group_entries'][token][0]
    original = group_order_service.lock_open
    state = {'done': False}

    def racing_lock(group, statuses=('open',)):
        if not state['done']:   # 主揪在參加者取得 entry 之後、鎖住團購之前移除並提交
            state['done'] = True
            db.session.execute(delete(GroupOrderEntry).where(GroupOrderEntry.group_order_id == gid)
                               .execution_options(synchronize_session=False))
            db.session.commit()
        return original(group, statuses)

    monkeypatch.setattr(group_order_service, 'lock_open', racing_lock)
    r = c.post(share, data={'action': 'update', 'entry_key': key, 'name': 'Amy2', f'qty_{rice}': '2'})
    assert r.status_code == 404 and '已被主揪移除' in r.get_data(as_text=True)


def test_manage_page_status_and_order_type_are_translated(app):
    """R2-177：英文主揪管理頁的狀態徽章與用餐方式不再是中文"""
    tid = _shop(app, 'fix-go2-en', languages=['en'])
    _gid, token, key = _group(app, tid, order_type='外帶')
    c = _manager(app, 'fix-go2-en', token, key)
    html = c.get(f'/shop/fix-go2-en/group/{token}/manage?lang=en').get_data(as_text=True)
    assert 'Take-out' in html and 'Open' in html
    assert '收單中' not in html and '外帶' not in html


def test_cancelled_order_is_shown_on_group_pages(app):
    """R2-178：送單後訂單被取消，團購頁不再顯示「已送單，請到店取餐付款」"""
    tid = _shop(app, 'fix-go2-cancel')
    rice = _item(app, tid, '修測取消飯')
    gid, token, key = _group(app, tid, entries=[('甲', [{'menu_item_id': rice, 'quantity': 1}])])
    with tenant_request(app, tid):
        grp, err = group_order_service.submit(gid)
        assert err == '' and grp.status == 'submitted'
        grp.order.status = 'Cancelled'
        db.session.commit()
    c = _manager(app, 'fix-go2-cancel', token, key)
    manage = c.get(f'/shop/fix-go2-cancel/group/{token}/manage').get_data(as_text=True)
    assert 'groupOrderCancelled' in manage and '此訂單已取消' in manage and 'groupSubmitted' not in manage
    join = c.get(f'/shop/fix-go2-cancel/group/{token}').get_data(as_text=True)
    assert '此訂單已取消' in join and '已送單，請等候主揪通知取餐' not in join


def test_join_page_lists_items_served_at_pickup_time(app):
    """B-017 / B-443：參加頁以取餐時間判斷供應星期，與送單檢查一致 (今天不供應、取餐日供應的品項要列出)"""
    tid = _shop(app, 'fix-go2-pickup-day')
    pickup = (datetime.now() + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    only_pickup_day = _item(app, tid, '修測取餐日限定便當', avail_days=str(pickup.isoweekday()))
    _gid, token, _key = _group(app, tid, pickup_at=pickup.strftime('%Y-%m-%dT%H:%M'))
    share = f'/shop/fix-go2-pickup-day/group/{token}'
    c = app.test_client()
    assert '修測取餐日限定便當' in c.get(share).get_data(as_text=True)
    c.post(share, data={'action': 'add', 'name': 'Amy', f'qty_{only_pickup_day}': '1'})
    assert_flash(c, '已加入')


def test_closed_message_in_english_has_space_and_capital(app):
    """R2-176：英文的截止 / 取消提示有空格、句首大寫"""
    tid = _shop(app, 'fix-go2-closed', languages=['en'])
    _gid, token, _key = _group(app, tid)
    with tenant_request(app, tid):
        grp = GroupOrder.query.filter_by(token=token).one()
        group_order_service.cancel(grp)
        db.session.commit()
    c = app.test_client()
    html = c.get(f'/shop/fix-go2-closed/group/{token}?lang=en').get_data(as_text=True)
    assert 'This group order has been cancelled.' in html
    zh = app.test_client().get(f'/shop/fix-go2-closed/group/{token}?lang=zh').get_data(as_text=True)
    assert '這個團購已取消。' in zh
