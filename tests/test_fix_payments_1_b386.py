"""B-386 回歸測試：record_payment() 寫入收款分店 (PaymentRecord.node_uuid)"""
from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.payment import PaymentKind, PaymentRecord
from app.services import payment_service
from app.sync import runtime as sync_runtime
from tests.support import default_tenant_id, tenant_request

NODE_UUID = '00000000-0000-0000-0000-00000000b386'


def _record(app, tid):
    with tenant_request(app, tid):
        rec = payment_service.record_payment(None, PaymentKind.PAY_IN, PaymentMethod.CASH, 100, tenant_id=tid,
                                             staff='system', note='B-386')
        db.session.commit()
        return rec.id


def test_node_payment_without_order_records_branch(app, monkeypatch):
    tid = default_tenant_id(app)
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    with app.app_context():
        sync_runtime.set_state('node_uuid', NODE_UUID)
        db.session.commit()
    rid = _record(app, tid)
    with app.app_context():
        assert db.session.get(PaymentRecord, rid).node_uuid == NODE_UUID


def test_standalone_and_hub_payment_node_uuid_empty(app, monkeypatch):
    tid = default_tenant_id(app)
    with app.app_context():
        sync_runtime.set_state('node_uuid', NODE_UUID)
        db.session.commit()
    for role in ('standalone', 'hub'):
        monkeypatch.setitem(app.config, 'SYNC_ROLE', role)
        rid = _record(app, tid)
        with app.app_context():
            assert db.session.get(PaymentRecord, rid).node_uuid is None
