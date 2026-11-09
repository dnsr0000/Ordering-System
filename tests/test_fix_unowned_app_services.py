"""跨檔請求 (app/services) 的回歸測試：B-018 / B-282 / B-347 / B-358 / R2-149 / B-451"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus, OrderType
from app.models.expense import CostHistory
from app.models.inventory import Ingredient
from app.models.member import OtpCode
from app.models.menu import MenuItem
from tests.support import default_tenant_id, tenant_request


def test_b018_facade_reexports_restock_demands():
    from app.services import order_service, order_stock
    assert order_service.restock_demands is order_stock.restock_demands


def test_b282_refresh_all_keeps_synced_carbon_without_recipe(app):
    from app.services import carbon_service
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        item = MenuItem(tenant_id=tid, name='B282 同步來的碳足跡', category='主餐', price=100, stock=10, total_stock=10,
                        carbon_auto=True, carbon_grams=850)
        db.session.add(item)
        db.session.commit()
        carbon_service.refresh_all(tid)
        db.session.commit()
        assert db.session.get(MenuItem, item.id).carbon_grams == 850


def _fake_order(**kw):
    user = SimpleNamespace(line_user_id='U-b347', tenant=SimpleNamespace(name='測試店'))
    base = dict(id=1, tenant_id=None, user=user, order_type=OrderType.HOME_DELIVERY, status=OrderStatus.COMPLETED,
                delivery_status='pending', display_pickup_number=7)
    base.update(kw)
    return SimpleNamespace(**base)


def test_b347_push_ready_home_delivery(app, monkeypatch, store_settings):
    from app.services import line_service, notify_service
    store_settings('notify', line_ready=True, line_ready_template='{number} 號請至櫃檯取餐')
    sent = []
    monkeypatch.setattr(line_service, 'push_text', lambda tid, uid, text: sent.append(text) or True)
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        holder = {}
        monkeypatch.setattr(db.session, 'get', lambda model, oid, **kw: holder['order'], raising=False)
        holder['order'] = _fake_order(tenant_id=tid)
        assert notify_service.push_ready(1)
        assert sent[-1] == '您的餐點 (取餐號 007) 已完成，外送員即將出發！'
        # 已送達 / 配送失敗 / 已取餐：不再通知
        for kw in ({'delivery_status': 'delivered'}, {'delivery_status': 'failed'}, {'status': OrderStatus.PICKED_UP}):
            holder['order'] = _fake_order(tenant_id=tid, **kw)
            assert notify_service.push_ready(1) is False
        assert len(sent) == 1
        # 非外送單維持店家範本
        holder['order'] = _fake_order(tenant_id=tid, order_type=OrderType.TAKE_OUT)
        assert notify_service.push_ready(1)
        assert sent[-1] == '007 號請至櫃檯取餐'


def test_b358_verify_otp_counts_attempts_atomically(app):
    from app.services import sms_service
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        otp = OtpCode(tenant_id=tid, phone='0966358358', purpose='register', code_hash=generate_password_hash('123456'),
                      expires_at=datetime.now() + timedelta(minutes=10), attempts=sms_service.OTP_MAX_ATTEMPTS - 1)
        db.session.add(otp)
        db.session.commit()
        oid = otp.id
        with pytest.raises(AppError) as exc:
            sms_service.verify_otp('0966358358', 'register', '000000')
        assert exc.value.status == 400
        db.session.rollback()
        assert db.session.get(OtpCode, oid).attempts == sms_service.OTP_MAX_ATTEMPTS
        # 次數用完：正確的碼也拒絕，計數不再增加
        with pytest.raises(AppError) as exc:
            sms_service.verify_otp('0966358358', 'register', '123456')
        assert exc.value.status == 429
        db.session.rollback()
        assert db.session.get(OtpCode, oid).attempts == sms_service.OTP_MAX_ATTEMPTS


def test_b358_verify_otp_success_still_counts(app):
    from app.services import sms_service
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        otp = OtpCode(tenant_id=tid, phone='0966358359', purpose='register', code_hash=generate_password_hash('654321'),
                      expires_at=datetime.now() + timedelta(minutes=10))
        db.session.add(otp)
        db.session.commit()
        got = sms_service.verify_otp('0966358359', 'register', '654321')
        assert got.id == otp.id and got.verified_at is not None and got.attempts == 1
        db.session.rollback()


def test_r2_149_deleting_ingredient_removes_cost_history(app):
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        ing = Ingredient(tenant_id=tid, name='R2149 原料', unit='g', unit_cost=1.0)
        db.session.add(ing)
        db.session.commit()
        ing.unit_cost = 2.0
        db.session.commit()
        iid = ing.id
        assert CostHistory.query.filter_by(tenant_id=tid, kind='ingredient', ref_id=iid).count() == 1
        db.session.delete(ing)
        db.session.commit()
        assert CostHistory.query.filter_by(tenant_id=tid, kind='ingredient', ref_id=iid).count() == 0


def test_b451_log_sms_event_is_tenant_tagged(app, store_settings):
    from app.services import monitor_service, sms_service
    store_settings('sms', provider='log')
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        assert sms_service.send_sms(tid, '0966451451', '驗證碼 451451') == 'logged'
    with app.app_context():
        event = next(e for e in monitor_service.recent_events(30) if '0966451451' in e['message'])
    assert event['tenant_id'] == tid and '451451' in event['message']
