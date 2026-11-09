"""桌邊線上結帳的修正回歸測試 (B-502、B-044)"""
import warnings

import pytest
from sqlalchemy import inspect as sa_inspect

from app.extensions import db
from app.models.table import DiningTable, TablePayment
from tests.support import checkout, line, make_item


def _table(app, admin_client, name):
    admin_client.post('/admin/tables', data={'action': 'create', 'names': name})
    with app.app_context():
        t = DiningTable.query.filter_by(name=name).one()
        return t.id, t.token


def _phone(app, token):
    c = app.test_client()
    c.get(f'/t/{token}')
    c.post('/guest_login')
    return c


def _start(app, admin_client, store_settings, name, price=100):
    store_settings('table_pay', enabled=True)
    item = make_item(app, f'{name}餐', price=price, stock=20)
    table_id, token = _table(app, admin_client, name)
    phone = _phone(app, token)
    checkout(phone, [line(item)])
    assert phone.post('/api/table/pay').status_code == 200
    with app.app_context():
        return table_id, TablePayment.query.filter_by(dining_table_id=table_id).one().trade_no


def _capture_alerts(monkeypatch):
    from app.services import monitor_service
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append((msg, kw)))
    return alerts


def test_missing_table_payment_raises_refund_alert(app, monkeypatch):
    """B-502：綠界通知找不到這筆結帳 → 回 missing 並發警示 (含綠界交易編號) 供人工退款"""
    from app.services import table_pay_service
    alerts = _capture_alerts(monkeypatch)
    with app.test_request_context('/'):
        assert table_pay_service.mark_paid('NOSUCHTRADE01', '2411090001', '360') == 'missing'
    assert len(alerts) == 1
    msg, kw = alerts[0]
    assert 'NOSUCHTRADE01' in msg and '2411090001' in msg and '360' in msg and '退款' in msg
    assert kw['key'] == 'table-pay-missing-NOSUCHTRADE01' and 'tenant_id' not in kw


def test_deleted_table_does_not_query_null_primary_key(app, admin_client, store_settings, monkeypatch):
    """B-502：桌位已刪除 (dining_table_id 為 NULL) 時不以 NULL 主鍵查詢桌位，仍照常完成結帳"""
    from app.services import table_pay_service
    _capture_alerts(monkeypatch)
    _table_id, trade_no = _start(app, admin_client, store_settings, 'FIXP3A')
    with app.app_context():
        cols = {c['name']: c for c in sa_inspect(db.engine).get_columns('table_payment')}
        if not cols['dining_table_id']['nullable']:
            pytest.skip('table_payment.dining_table_id 尚未改為可為 NULL 的遷移 (B-502 由其他小組新增)')
        tp = TablePayment.query.filter_by(trade_no=trade_no).one()
        tp.dining_table_id = None
        db.session.commit()
    with app.test_request_context('/'), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        result = table_pay_service.mark_paid(trade_no, '2411090002', 100)
    assert not [w for w in caught if 'NULL primary key' in str(w.message)], [str(w.message) for w in caught]
    assert result == 'paid'


def test_unexpected_settle_error_marks_conflict_and_alerts(app, admin_client, store_settings, monkeypatch):
    """B-044：結帳時發生非預期錯誤 → 不停在 processing，改為 conflict 並通知店家退款"""
    from app.services import table_pay_service, table_service
    alerts = _capture_alerts(monkeypatch)
    _table_id, trade_no = _start(app, admin_client, store_settings, 'FIXP3B')

    def boom(*a, **kw):
        raise RuntimeError('db gone')
    monkeypatch.setattr(table_service, 'settle_table', boom)
    with app.test_request_context('/'):
        assert table_pay_service.mark_paid(trade_no, '2411090003', 100) == 'conflict'
    with app.app_context():
        tp = TablePayment.query.filter_by(trade_no=trade_no).one()
        assert tp.status == 'conflict' and tp.provider_trade_no == '2411090003' and tp.paid_at is not None
        assert 'RuntimeError' in tp.note
    assert any('2411090003' in msg and '退款' in msg for msg, _kw in alerts), alerts


def test_claim_records_paid_at_and_provider_trade_no(app, admin_client, store_settings, monkeypatch):
    """B-044：搶到處理權時就記下 paid_at 與綠界交易編號 (行程中斷停在 processing 時，定期工作據此判斷逾時、通知退款)"""
    from app.services import table_pay_service, table_service
    _capture_alerts(monkeypatch)
    _table_id, trade_no = _start(app, admin_client, store_settings, 'FIXP3C')
    seen = {}

    def peek(*a, **kw):
        tp = db.session.execute(db.select(TablePayment.status, TablePayment.paid_at, TablePayment.provider_trade_no)
                                .where(TablePayment.trade_no == trade_no)
                                .execution_options(skip_tenant_filter=True)).one()
        seen.update(status=tp[0], paid_at=tp[1], provider=tp[2])
        raise RuntimeError('crash mid-settle')
    monkeypatch.setattr(table_service, 'settle_table', peek)
    with app.test_request_context('/'):
        table_pay_service.mark_paid(trade_no, '2411090004', 100)
    assert seen['status'] == 'processing' and seen['paid_at'] is not None and seen['provider'] == '2411090004'
