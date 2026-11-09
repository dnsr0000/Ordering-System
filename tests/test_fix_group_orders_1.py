"""修正小組 group-orders-1 的回歸測試：團體預購送單、截止時間、供應時段、取消競態、備註"""
from datetime import datetime, timedelta

import pytest
from flask import has_request_context

from app.errors import AppError
from app.extensions import db
from app.models.group_order import GroupOrder
from app.models.menu import MenuItem, ModifierOption
from app.models.order import Order
from app.models.payment import PaymentRecord
from app.services import group_order_service as svc
from app.services import monitor_service, settings_service
from tests.support import get_or_create_tenant, tenant_request


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


def _setup(app, slug, **over):
    tid = get_or_create_tenant(app, slug)
    with tenant_request(app, tid):
        cfg = dict(settings_service.get('group_order', tid), **dict(dict(enabled=True, min_lead_minutes=60), **over))
        settings_service.put('group_order', cfg, tenant_id=tid)
        db.session.commit()
    return tid


def _item(app, tid, name, price=100, **kw):
    with tenant_request(app, tid):
        m = MenuItem(tenant_id=tid, name=name, price=price, stock=kw.pop('stock', 99), total_stock=99, category='便當', **kw)
        db.session.add(m)
        db.session.commit()
        return m.id


def _pickup(hours=5):
    t = datetime.now() + timedelta(hours=hours)
    return t.replace(minute=0, second=0, microsecond=0)


def _group(app, tid, entries, pickup=None, auto=True, title='團購'):
    pickup = pickup or _pickup()
    with tenant_request(app, tid):
        grp, _key = svc.create(tid, {'title': title, 'organizer_name': '主揪', 'organizer_phone': '0911000111',
                                     'pickup_at': pickup.strftime('%Y-%m-%dT%H:%M'),
                                     'auto_submit': '1' if auto else '0'})
        db.session.flush()
        for name, lines in entries:
            svc.save_entry(grp, name, lines)
        db.session.commit()
        return grp.id


def test_background_run_due_submits_without_request_context(app, alerts):
    """B-441 / B-440：背景排程只有 app context 也能送單；團購為到店付款，不記收款紀錄"""
    tid = _setup(app, 'fix-go1-bg')
    rice = _item(app, tid, '背景送單飯')
    gid = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 2}])])
    with app.app_context():
        grp = db.session.get(GroupOrder, gid)
        after_deadline = grp.deadline + timedelta(minutes=1)
        assert not has_request_context()
        assert svc.run_due(tid, now=after_deadline) == (1, 0)
        assert not has_request_context()
        grp = db.session.get(GroupOrder, gid)
        assert grp.status == 'submitted', grp.error
        order = db.session.get(Order, grp.order_id)
        assert order.tenant_id == tid and sum(i.quantity for i in order.items) == 2
        assert order.payment_status == 'unpaid'
        assert PaymentRecord.query.filter_by(order_id=order.id).count() == 0


def test_submit_ignores_kiosk_payment_methods(app, alerts):
    """B-444：點餐機不收現金時，團購 (到店付款) 仍可送單"""
    tid = _setup(app, 'fix-go1-pay')
    rice = _item(app, tid, '不收現金飯')
    with tenant_request(app, tid):
        cfg = dict(settings_service.get('payments', tid), kiosk_methods=['Credit Card'])
        settings_service.put('payments', cfg, tenant_id=tid)
        db.session.commit()
    gid = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1}])])
    with tenant_request(app, tid):
        grp, err = svc.submit(gid)
        assert err == '' and grp.status == 'submitted'


def test_failure_before_deadline_keeps_group_open(app, alerts):
    """B-445：截止前送單失敗 → 恢復收單，參加者仍可加點 / 修改；截止後失敗才是 failed"""
    tid = _setup(app, 'fix-go1-open')
    rice = _item(app, tid, '提早送單飯')
    gid = _group(app, tid, [])
    with tenant_request(app, tid):
        grp, err = svc.submit(gid)
        assert err == '沒有人點餐' and grp.status == 'open' and grp.error == '沒有人點餐'
        grp = db.session.get(GroupOrder, gid)
        svc.save_entry(grp, '乙', [{'menu_item_id': rice, 'quantity': 1}])
        db.session.commit()
        # 截止後失敗：標示送單失敗
        db.session.get(MenuItem, rice).is_sold_out = True
        db.session.commit()
        grp, err = svc.submit(gid, now=grp.deadline + timedelta(minutes=1))
        assert err and grp.status == 'failed'


def test_menu_and_parse_lines_use_pickup_time(app, alerts):
    """B-443：只在取餐那一天供應的餐點，前一天揪團也點得到"""
    tid = _setup(app, 'fix-go1-svc')
    pickup = (datetime.now() + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    only_that_day = _item(app, tid, '指定日便當', avail_days=str(pickup.isoweekday()))
    gid = _group(app, tid, [], pickup=pickup)
    with tenant_request(app, tid):
        grp = db.session.get(GroupOrder, gid)
        assert only_that_day not in {m.id for m in svc.menu_items(tid)}   # 今天不供應
        assert only_that_day in {m.id for m in svc.menu_items(tid, grp.pickup_at)}
        lines = svc.parse_lines(tid, grp, {f'qty_{only_that_day}': '2'})
        assert lines[0]['menu_item_id'] == only_that_day and lines[0]['quantity'] == 2


def test_blank_deadline_falls_back_when_default_already_passed(app):
    """B-447：取餐時間較近、預設截止時間已過時，改用現在之後最近的 15 分鐘整點"""
    tid = _setup(app, 'fix-go1-dl', min_lead_minutes=30, deadline_minutes=60)
    now = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=10, second=0, microsecond=0)
    with tenant_request(app, tid):
        grp, _key = svc.create(tid, {'title': '近取餐', 'organizer_name': '主揪', 'organizer_phone': '0911000111',
                                     'pickup_at': (now + timedelta(minutes=50)).strftime('%Y-%m-%dT%H:%M')}, now=now)
        assert grp.deadline == now.replace(minute=15)
        db.session.rollback()


def test_unexpected_error_message_is_generic(app, alerts, monkeypatch):
    """B-448：非預期例外的內容不顯示在主揪管理頁 (只進 log / 監控通知)"""
    from app.services import order_service
    tid = _setup(app, 'fix-go1-err')
    rice = _item(app, tid, '出錯飯')
    gid = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1}])])

    def broken(**kw):
        raise RuntimeError('SELECT secret FROM internal')
    monkeypatch.setattr(order_service, 'process_checkout', broken)
    with tenant_request(app, tid):
        grp, err = svc.submit(gid)
        assert grp.status == 'failed' and grp.error == svc.SYSTEM_ERROR and 'secret' not in grp.error
        assert any('secret' in a for a in alerts)


def test_cancel_is_conditional_and_checks_existing_order(app, alerts):
    """B-450：取消與送單同時發生不會蓋掉已送單；failed 但訂單已建立的團購不能取消"""
    from sqlalchemy.orm.attributes import set_committed_value
    tid = _setup(app, 'fix-go1-cancel')
    rice = _item(app, tid, '取消飯')
    gid = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1}])])
    with tenant_request(app, tid):
        grp, err = svc.submit(gid)
        assert err == ''
        # 取消請求讀到的是送單前的狀態
        set_committed_value(grp, 'status', 'open')
        with pytest.raises(AppError):
            svc.cancel(grp)
        db.session.commit()
        assert db.session.get(GroupOrder, gid).status == 'submitted'
        # 送單中斷被標成 failed，但訂單其實已建立
        db.session.get(GroupOrder, gid).status = 'failed'
        db.session.commit()
        with pytest.raises(AppError, match='訂單已經建立'):
            svc.cancel(db.session.get(GroupOrder, gid))
        db.session.rollback()
        assert db.session.get(GroupOrder, gid).status == 'failed'
    # 一般取消
    other = _group(app, tid, [], title='要取消')
    with tenant_request(app, tid):
        grp = db.session.get(GroupOrder, other)
        svc.cancel(grp)
        db.session.commit()
        assert grp.status == 'cancelled' and db.session.get(GroupOrder, other).status == 'cancelled'


def test_note_with_modifier_name_reaches_order_note(app, alerts):
    """R2-175：備註含加料名稱 (「不要加荷包蛋」) 會被明細移除，改寫進訂單備註讓廚房看得到"""
    tid = _setup(app, 'fix-go1-note')
    rice = _item(app, tid, '荷包蛋飯')
    with tenant_request(app, tid):
        db.session.add(ModifierOption(tenant_id=tid, name='加荷包蛋', price=15, is_active=True))
        db.session.commit()
    gid = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1, 'size': '',
                                     'customization': '不要加荷包蛋、少辣'}])])
    with tenant_request(app, tid):
        grp, err = svc.submit(gid)
        assert err == ''
        order = db.session.get(Order, grp.order_id)
        assert '荷包蛋飯:不要加荷包蛋' in order.note and '甲:' in order.note
        assert order.note.startswith('團購「團購」主揪 0911000111｜')


def test_run_due_counts_only_successful_submits(app, alerts):
    """B-004 / B-026：自動送單失敗 (沒有人點餐) 不算進已送數"""
    tid = _setup(app, 'fix-go1-count')
    rice = _item(app, tid, '計數飯')
    ok = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1}])], title='會成功')
    empty = _group(app, tid, [], title='沒人點')
    with app.app_context():
        after = db.session.get(GroupOrder, ok).deadline + timedelta(minutes=1)
        assert svc.run_due(tid, now=after) == (1, 0)
        assert db.session.get(GroupOrder, ok).status == 'submitted'
        assert db.session.get(GroupOrder, empty).status == 'failed'


def test_scheduled_submit_skips_intake_but_manual_is_checked(app, alerts, monkeypatch):
    """B-131：暫停接單時，主揪手動送單被擋；排程自動送單 (截止時已承諾) 仍會送出"""
    from app.services import intake_service
    tid = _setup(app, 'fix-go1-intake')
    rice = _item(app, tid, '暫停飯')

    def paused(c):
        raise AppError('暫停接單中', 409, intake_paused=True)
    monkeypatch.setattr(intake_service, 'check', paused)
    manual = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1}])], title='手動')
    with tenant_request(app, tid):
        grp, err = svc.submit(manual)
        assert err == '暫停接單中' and grp.order_id is None
    auto = _group(app, tid, [('乙', [{'menu_item_id': rice, 'quantity': 1}])], title='自動')
    with tenant_request(app, tid):
        grp = db.session.get(GroupOrder, auto)
        db.session.get(GroupOrder, manual).auto_submit = False
        db.session.commit()
        assert svc.run_due(tid, now=grp.deadline + timedelta(minutes=1)) == (1, 0)
        assert db.session.get(GroupOrder, auto).status == 'submitted'


def test_disabled_feature_does_not_auto_submit(app, alerts):
    """R2-170：功能關閉後，截止的自動送單團購不送單、通知店家，取餐時間過後取消"""
    tid = _setup(app, 'fix-go1-off')
    rice = _item(app, tid, '關閉飯')
    gid = _group(app, tid, [('甲', [{'menu_item_id': rice, 'quantity': 1}])])
    with tenant_request(app, tid):
        cfg = dict(settings_service.get('group_order', tid), enabled=False)
        settings_service.put('group_order', cfg, tenant_id=tid)
        db.session.commit()
        grp = db.session.get(GroupOrder, gid)
        deadline, pickup = grp.deadline, grp.pickup_at
        assert svc.run_due(tid, now=deadline + timedelta(minutes=1)) == (0, 0)
        assert db.session.get(GroupOrder, gid).status == 'open'
        assert any('團體預購已關閉' in a for a in alerts)
        assert svc.run_due(tid, now=pickup + timedelta(minutes=1)) == (0, 1)
        assert db.session.get(GroupOrder, gid).status == 'cancelled'
        assert Order.query.filter_by(client_order_id=grp.uuid).count() == 0
