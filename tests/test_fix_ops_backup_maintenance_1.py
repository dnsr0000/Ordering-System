"""跨檔請求修正 (ops-backup-maintenance-1) 的回歸測試：B-044 桌邊線上結帳卡在 processing 時改為 conflict 並告警"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.table import TablePayment
from app.services import maintenance_service, monitor_service
from tests.support import default_tenant_id


def _payment(tid, trade_no, paid_at, created_at, status='processing'):
    tp = TablePayment(tenant_id=tid, dining_table_id=None, amount=480, trade_no=trade_no, status=status,
                      provider_trade_no='2411090999' if paid_at else '', paid_at=paid_at, order_ids='1')
    db.session.add(tp)
    db.session.flush()
    tp.created_at = created_at
    db.session.commit()
    return tp.id


def test_stuck_table_payments_become_conflict(app, monkeypatch):
    """B-044：paid_at 超過 10 分鐘 (或舊資料 paid_at 為空、建立超過 60 分鐘) 的 processing 改為 conflict 並告警；其他不動"""
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append((msg, kw)))
    now = datetime(2026, 11, 9, 12, 0)
    with app.app_context():
        tid = default_tenant_id(app)
        stuck = _payment(tid, 'OBM1STUCK01', now - timedelta(minutes=11), now - timedelta(minutes=20))
        fresh = _payment(tid, 'OBM1FRESH01', now - timedelta(minutes=5), now - timedelta(minutes=20))
        legacy = _payment(tid, 'OBM1LEGACY1', None, now - timedelta(minutes=61))
        legacy_new = _payment(tid, 'OBM1LEGNEW1', None, now - timedelta(minutes=30))
        paid = _payment(tid, 'OBM1PAID001', now - timedelta(minutes=30), now - timedelta(minutes=40), status='paid')

        done = maintenance_service.recover_stuck_table_payments(now=now)
        assert sorted(done) == sorted([stuck, legacy])
        db.session.expire_all()
        status = {r.id: (r.status, r.note) for r in TablePayment.query.execution_options(skip_tenant_filter=True)
                  .filter(TablePayment.id.in_([stuck, fresh, legacy, legacy_new, paid])).all()}
        assert status[stuck][0] == 'conflict' and '結帳中斷' in status[stuck][1]
        assert status[legacy][0] == 'conflict'
        assert status[fresh][0] == 'processing'
        assert status[legacy_new][0] == 'processing'
        assert status[paid][0] == 'paid'
        assert len(alerts) == 2
        msg, kw = next(a for a in alerts if a[1]['key'] == f'table-pay-stuck-{stuck}')
        assert 'OBM1STUCK01' in msg and '2411090999' in msg
        assert kw['tenant_id'] == tid and kw['level'] == 'warning' and kw['source'] == 'payment'
        legacy_msg = next(a[0] for a in alerts if a[1]['key'] == f'table-pay-stuck-{legacy}')
        assert '未知' in legacy_msg

        # 再跑一次不會重複處理
        assert maintenance_service.recover_stuck_table_payments(now=now) == []
        assert len(alerts) == 2
