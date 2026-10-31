"""
資安審查第二輪 (2026-10-30) 的修正：
1. 整庫備份 (系統監控頁) 只有平台管理者可建立 / 下載；門市主機預設不開放公開建立店家
2. 綠界公開測試金鑰需明確允許 (ECPAY_ALLOW_TEST_KEYS)，否則付款通知視為檢查碼錯誤、不產生付款表單
3. LINE 待綁定資料登出即清除、10 分鐘失效、可取消
4. 打卡頁對未登入者遮罩員工帳號；已註冊的店內裝置不受帳號層級鎖定影響 (無法被外人鎖死)
5. POS 動用儲值金 / 點數 / 寄杯需會員驗證 (動態條碼 / PIN / 主管核准)
6. client_order_id 存獨立欄位，訂單 uuid 由伺服器產生
7. 監控 Webhook 與線上端的寄信 / 校時 / 印表機主機不可指向內網
8. 簡訊驗證碼有全店上限
"""
import io
import re
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest

from tests.support import (default_tenant_id, guest_client, line, login_staff, make_item, make_member, member_client,
                           other_shop_owner_client)

UUID4_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')


@pytest.fixture
def config_flag(app):
    """暫時改一個 app.config 值，測試結束還原"""
    saved = {}

    def setter(key, value):
        saved.setdefault(key, app.config.get(key))
        app.config[key] = value
    yield setter
    for key, value in saved.items():
        app.config[key] = value


# ---------------------------------------------------------------------------
# 1. 整庫備份與公開建店
# ---------------------------------------------------------------------------
def test_sqlite_backup_only_for_platform_admin(app, admin_client):
    other = other_shop_owner_client(app, slug='audit2-backup-shop')
    assert other.post('/admin/system/backup').status_code == 403
    assert other.get('/admin/system/backup/menu-20261030-120000.zip').status_code == 403
    html = other.get('/admin/system').get_data(as_text=True)
    assert 'menu-' not in html and '僅平台管理者' in html
    # 平台管理者 (預設店家負責人) 仍可建立、看到清單並下載
    r = admin_client.post('/admin/system/backup')
    assert r.status_code in (302, 303)
    names = re.findall(r'menu-\d{8}-\d{6}\.zip', admin_client.get('/admin/system').get_data(as_text=True))
    assert names
    dl = admin_client.get(f'/admin/system/backup/{names[0]}')
    assert dl.status_code == 200 and 'menu.db' in zipfile.ZipFile(io.BytesIO(dl.data)).namelist()
    # 其他店負責人拿到檔名也不能下載
    assert other.get(f'/admin/system/backup/{names[0]}').status_code == 403


def test_tenant_register_closed_unless_allowed_or_platform_admin(app, admin_client, config_flag):
    config_flag('ALLOW_TENANT_REGISTER', False)
    c = app.test_client()
    form = {'shop_name': '未授權店', 'slug': 'audit2-closed', 'username': 'audit2_closed_owner', 'password': 'ClosedPass!2026'}
    assert c.post('/tenant/register', data=form, headers={'X-Forwarded-For': '203.0.113.50'}).status_code == 403
    r = c.get('/tenant/register')
    assert r.status_code in (302, 303)   # 導回登入頁並說明
    assert '建立新店家帳號' not in c.get('/admin').get_data(as_text=True)
    # 其他店的負責人也不行 (不是平台管理者)
    other = other_shop_owner_client(app, slug='audit2-not-admin')
    assert other.post('/tenant/register', data=form).status_code == 403
    # 平台管理者可以建立，且維持自己的登入 (不會變成新店負責人)
    r = admin_client.post('/tenant/register', data=form)
    assert r.status_code in (302, 303)
    with app.app_context():
        from app.models.tenant import StaffAccount, Tenant
        t = Tenant.query.filter_by(slug='audit2-closed').first()
        assert t is not None and StaffAccount.query.filter_by(username='audit2_closed_owner', tenant_id=t.id).first()
    html = admin_client.get('/admin').get_data(as_text=True)
    assert 'admin_test' in html and 'nav-tenant-register' in html


# ---------------------------------------------------------------------------
# 2. 綠界測試金鑰
# ---------------------------------------------------------------------------
def _forged_notify(order_uuid, total):
    from app.services import payment_ecpay
    key, iv = payment_ecpay.TEST_MERCHANT[1], payment_ecpay.TEST_MERCHANT[2]
    params = {'MerchantID': '3002607', 'MerchantTradeNo': 'KFAKE000000000000001', 'RtnCode': '1', 'RtnMsg': '交易成功',
              'TradeNo': '2026103000000001', 'TradeAmt': str(total), 'PaymentDate': '2026/10/30 12:00:00',
              'PaymentType': 'Credit_CreditCard', 'CustomField1': order_uuid, 'SimulatePaid': '0'}
    params['CheckMacValue'] = payment_ecpay.check_mac_value(params, key, iv)
    return params


def test_public_test_keys_rejected_unless_explicitly_allowed(app, config_flag):
    from app.extensions import db
    from app.models.order import Order
    from app.tenant_context import tenant_bypass
    config_flag('SYNC_ROLE', 'hub')
    config_flag('ECPAY_ALLOW_TEST_KEYS', False)
    with app.app_context():
        order = Order(tenant_id=default_tenant_id(app), total_price=250, payment_status='pending',
                      order_source='Online', status='Pending', payment_method='Cash')
        with tenant_bypass():
            db.session.add(order)
            db.session.commit()
        order_uuid = order.uuid
    c = app.test_client()
    r = c.post('/payment/ecpay/notify', data=_forged_notify(order_uuid, 250))
    assert r.get_data(as_text=True) == '0|CheckMacValue Error'
    assert c.post('/payment/ecpay/result', data=_forged_notify(order_uuid, 250)).status_code == 400
    assert c.post('/payment/ecpay/table_result', data=_forged_notify('T:KFAKE', 250)).status_code == 400
    with app.app_context():
        with tenant_bypass():
            assert Order.query.filter_by(uuid=order_uuid).one().payment_status == 'pending'
    # 明確允許後 (測試環境) 才接受
    config_flag('ECPAY_ALLOW_TEST_KEYS', True)
    assert c.post('/payment/ecpay/notify', data=_forged_notify(order_uuid, 250)).get_data(as_text=True) == '1|OK'
    with app.app_context():
        with tenant_bypass():
            assert Order.query.filter_by(uuid=order_uuid).one().payment_status == 'paid'


def test_online_checkout_and_table_pay_refuse_to_start_with_blocked_test_keys(app, config_flag, store_settings):
    from app.errors import AppError
    from app.services import payment_ecpay, table_pay_service
    config_flag('ECPAY_ALLOW_TEST_KEYS', False)
    with app.test_request_context('/'):
        with pytest.raises(AppError) as exc:
            payment_ecpay.require_keys_allowed()
        assert exc.value.status == 503 and 'ECPAY_ALLOW_TEST_KEYS' in exc.value.message
        assert payment_ecpay.verify({'CheckMacValue': 'X'}) is False
        assert payment_ecpay.query_trade('K000') is None
    # 桌邊線上結帳：不建立付款紀錄
    store_settings('table_pay', enabled=True)
    with app.app_context():
        from app.models.table import DiningTable, TablePayment
        from app.tenant_context import tenant_bypass
        from flask import g
        g.tenant_id = default_tenant_id(app)
        with tenant_bypass():
            table = DiningTable.query.first() or DiningTable(tenant_id=g.tenant_id, name='AUDIT2', token=uuid.uuid4().hex)
            if table.id is None:
                from app.extensions import db
                db.session.add(table)
                db.session.commit()
            before = TablePayment.query.count()
            with pytest.raises(AppError) as exc:
                table_pay_service.start(table)
            assert exc.value.status == 503
            assert TablePayment.query.count() == before


# ---------------------------------------------------------------------------
# 3. LINE 待綁定
# ---------------------------------------------------------------------------
def _pending(tid, at=None, name='attacker'):
    return {'user_id': 'U-audit2-line', 'name': name, 'tenant_id': tid, 'at': (at or datetime.now()).isoformat()}


def _login_victim(c, phone, pin):
    return c.post('/phone_login', json={'phone': phone, 'pin': pin}, headers={'X-Forwarded-For': '10.42.0.9'})


def test_line_pending_cleared_on_logout_and_expires(app):
    from app.extensions import db
    from app.models.user import User
    tid = default_tenant_id(app)
    uid = make_member(app, '受害者甲', '0987000111', pin='7391')
    c = app.test_client()
    c.get('/?shop=default')
    with c.session_transaction() as sess:
        sess['line_pending'] = _pending(tid)
    c.post('/logout')   # 攻擊者離開 (或閒置登出)
    with c.session_transaction() as sess:
        assert 'line_pending' not in sess
    assert _login_victim(c, '0987000111', '7391').status_code == 200
    with app.app_context():
        assert (db.session.get(User, uid).line_user_id or '') == ''
    # 逾時的待綁定也不綁
    c.post('/logout')
    with c.session_transaction() as sess:
        sess['line_pending'] = _pending(tid, at=datetime.now() - timedelta(minutes=11))
    assert _login_victim(c, '0987000111', '7391').status_code == 200
    with app.app_context():
        assert (db.session.get(User, uid).line_user_id or '') == ''
    # 10 分鐘內、同一個瀏覽器接著登入：才綁定 (正常流程)
    c.post('/logout')
    with c.session_transaction() as sess:
        sess['line_pending'] = _pending(tid, at=datetime.now() - timedelta(minutes=2))
    html = c.get('/face_login').get_data(as_text=True)
    assert 'linePendingDiscard' in html and 'attacker' in html
    assert _login_victim(c, '0987000111', '7391').status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).line_user_id == 'U-audit2-line'


def test_line_pending_discard_route(app):
    tid = default_tenant_id(app)
    c = app.test_client()
    c.get('/?shop=default')
    with c.session_transaction() as sess:
        sess['line_pending'] = _pending(tid)
    r = c.post('/auth/line/pending/discard')
    assert r.status_code in (302, 303)
    with c.session_transaction() as sess:
        assert 'line_pending' not in sess


# ---------------------------------------------------------------------------
# 4. 打卡頁與帳號鎖定
# ---------------------------------------------------------------------------
def _register_device(app, tid):
    """在後台建立裝置並配對，回傳裝置 Cookie 原始值"""
    from app.extensions import db
    from app.services import device_service
    with app.app_context():
        from flask import g
        g.tenant_id = tid
        d = device_service.create(tid, 'audit2 POS', 'pos')
        db.session.commit()
        d, raw = device_service.pair(tid, d.pair_code)
        db.session.commit()
        return raw


def test_timeclock_masks_usernames_for_anonymous(app, admin_client):
    from app.extensions import db
    from app.models.staff import TimeClock
    from app.models.tenant import StaffAccount
    with app.app_context():
        staff = StaffAccount.query.filter_by(username='admin_test').first()
        db.session.add(TimeClock(tenant_id=staff.tenant_id, staff_account_id=staff.id, clock_in=datetime.now()))
        db.session.commit()
    c = app.test_client()
    c.get('/?shop=default')
    html = c.get('/timeclock').get_data(as_text=True)
    assert 'admin_test' not in html and 'a○' in html
    assert 'admin_test' in admin_client.get('/timeclock').get_data(as_text=True)
    # 已註冊的店內裝置也看得到完整帳號
    raw = _register_device(app, default_tenant_id(app))
    dev = app.test_client()
    dev.set_cookie('kiosk_device', raw)
    dev.get('/?shop=default')
    assert 'admin_test' in dev.get('/timeclock').get_data(as_text=True)


def test_registered_device_not_blocked_by_account_level_lockout(app):
    from app.extensions import db
    from app.models.tenant import StaffAccount
    with app.app_context():
        s = StaffAccount(tenant_id=default_tenant_id(app), username='audit2_locked_staff', role='staff', is_active=True)
        s.set_password('LockedPass!2026')
        db.session.add(s)
        db.session.commit()
    attacker = app.test_client()
    for i in range(10):   # 外人用 10 個 IP 各猜錯一次 → 帳號層級鎖定
        attacker.post('/admin', data={'username': 'audit2_locked_staff', 'password': 'wrong'},
                      headers={'X-Forwarded-For': f'203.0.113.{i}'})
    outside = app.test_client().post('/admin', data={'username': 'audit2_locked_staff', 'password': 'LockedPass!2026'},
                                     headers={'X-Forwarded-For': '198.51.100.7'})
    assert outside.status_code == 429
    # 店內已註冊的 POS 仍可正常登入 (只受同 IP 同帳號 5 次的裝置層級鎖定)
    raw = _register_device(app, default_tenant_id(app))
    pos = app.test_client()
    pos.set_cookie('kiosk_device', raw)
    r = pos.post('/admin', data={'username': 'audit2_locked_staff', 'password': 'LockedPass!2026'},
                 headers={'X-Forwarded-For': '192.168.1.20'})
    assert r.status_code in (302, 303)
    for _ in range(5):   # 裝置層級：同一台連錯 5 次仍會鎖
        pos.post('/admin', data={'username': 'audit2_locked_staff', 'password': 'wrong'},
                 headers={'X-Forwarded-For': '192.168.1.21'})
    r = pos.post('/admin', data={'username': 'audit2_locked_staff', 'password': 'LockedPass!2026'},
                 headers={'X-Forwarded-For': '192.168.1.21'})
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# 5. POS 會員驗證
# ---------------------------------------------------------------------------
@pytest.fixture
def verify_setup(app, store_settings):
    store_settings('member', wallet_enabled=True, pos_verify=True)
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.models.user import User
    from app.tenant_context import tenant_bypass
    with app.app_context():
        with tenant_bypass():
            existing = MenuItem.query.filter_by(name='audit2 驗證餐點').first()
            item = existing.id if existing else None
            user = User.query.filter_by(phone='0911222444').first()
            if user is not None:   # 每個測試都從 1000 元儲值金開始
                user.wallet_balance, user.points = 1000, 500
                db.session.commit()
                uid = user.id
            else:
                uid = None
    item = item or make_item(app, 'audit2 驗證餐點', price=120, stock=500)
    uid = uid or make_member(app, '儲值會員乙', '0911222444', pin='8264', wallet_balance=1000, points=500)
    return item, uid


def _wallet_payload(item, uid, **extra):
    return {'items': [line(item)], 'payment_method': '儲值金', 'user_id': uid, 'order_type': '內用', **extra}


def test_pos_wallet_requires_member_verification(app, verify_setup):
    from app.extensions import db
    from app.models.user import User
    item, uid = verify_setup
    staff = login_staff(app, 'audit2_cashier', 'staff')
    r = staff.post('/api/pos/checkout', json=_wallet_payload(item, uid))
    assert r.status_code == 403 and r.get_json()['need_member_pin'] is True and r.get_json()['has_pin'] is True
    # 點數折抵、混合付款的儲值金、寄杯也要驗證；單純現金不用
    r = staff.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'user_id': uid,
                                              'order_type': '內用', 'use_points': 10})
    assert r.status_code == 403 and r.get_json()['need_member_pin']
    r = staff.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': '混合付款', 'user_id': uid,
                                              'order_type': '內用', 'payment_info': {'tenders': [
                                                  {'method': '儲值金', 'amount': 100}, {'method': 'Cash', 'amount': 20}]}})
    assert r.status_code == 403 and r.get_json()['need_member_pin']
    assert staff.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'user_id': uid,
                                                 'order_type': '內用'}).status_code == 200
    # PIN 錯誤：拒絕並計入鎖定
    r = staff.post('/api/pos/checkout', json=_wallet_payload(item, uid, member_pin='0000'))
    assert r.status_code == 403 and 'PIN' in r.get_json()['message']
    # PIN 正確：成立訂單並扣儲值金；同一個收銀工作階段之後不必再輸入
    r = staff.post('/api/pos/checkout', json=_wallet_payload(item, uid, member_pin='8264'))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert staff.post('/api/pos/checkout', json=_wallet_payload(item, uid)).status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).wallet_balance == 1000 - 240


def test_pos_member_qr_scan_counts_as_verification(app, verify_setup):
    from app.extensions import db
    from app.models.user import User
    from app.services import member_qr_service
    item, uid = verify_setup
    with app.app_context():
        user = db.session.get(User, uid)
        token = member_qr_service.make_token(user)
        db.session.commit()
    staff = login_staff(app, 'audit2_cashier_qr', 'staff')
    r = staff.post('/api/pos/member_lookup', json={'query': token})
    assert r.status_code == 200 and r.get_json()['via_qr']
    assert staff.post('/api/pos/checkout', json=_wallet_payload(item, uid)).status_code == 200


def test_pos_member_verification_override_needs_wallet_permission(app, verify_setup):
    item, uid = verify_setup
    staff = login_staff(app, 'audit2_cashier_ovr', 'staff')
    # 一般店員按「主管核准」→ 需主管帳密
    r = staff.post('/api/pos/checkout', json=_wallet_payload(item, uid, member_verify_override=True))
    assert r.status_code == 403 and r.get_json().get('need_approval')
    r = staff.post('/api/pos/checkout', json=_wallet_payload(item, uid, approver_username='admin_test',
                                                             approver_password='AdminPass!2026'))
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        from app.models.user import StaffAuditLog
        assert StaffAuditLog.query.filter_by(action_type='MEMBER_VERIFY_OVERRIDE').count() >= 1
    # 尚未設定 PIN 的會員：提示 has_pin=False，PIN 路徑不可用
    uid2 = make_member(app, '無PIN會員', '0911222555', wallet_balance=300)
    r = staff.post('/api/pos/checkout', json=_wallet_payload(item, uid2, member_pin='1234'))
    assert r.status_code == 403 and r.get_json()['has_pin'] is False


def test_pos_member_verification_can_be_disabled(app, verify_setup, store_settings):
    item, uid = verify_setup
    store_settings('member', pos_verify=False)
    staff = login_staff(app, 'audit2_cashier_off', 'staff')
    assert staff.post('/api/pos/checkout', json=_wallet_payload(item, uid)).status_code == 200


# ---------------------------------------------------------------------------
# 6. client_order_id 與 uuid
# ---------------------------------------------------------------------------
def test_order_uuid_is_server_generated_and_client_id_dedups(app):
    from app.extensions import db
    from app.models.order import Order
    item = make_item(app, 'audit2 uuid 餐點', price=30, stock=50)
    c = guest_client(app)
    cid = 'attacker-chosen-id-00000000000000000'   # 36 字、非 uuid 格式
    a = c.post('/submit_order', json={'items': [line(item)], 'client_order_id': cid}).get_json()
    b = c.post('/submit_order', json={'items': [line(item)], 'client_order_id': cid}).get_json()
    assert a['order_id'] == b['order_id'] and b['replayed'] is True
    with app.app_context():
        o = db.session.get(Order, a['order_id'])
        assert o.client_order_id == cid and o.uuid != cid and UUID4_RE.match(o.uuid)
        # 追蹤頁只認伺服器產生的 uuid
        assert Order.query.filter_by(uuid=cid).first() is None
    assert c.get(f'/track/{cid}').status_code == 404
    with app.app_context():
        real = db.session.get(Order, a['order_id']).uuid
    assert c.get(f'/track/{real}').status_code == 200


def test_client_order_id_not_synced(app):
    from app.sync.registry import SPECS
    spec = next(s for s in SPECS if s.name == 'order')
    assert 'client_order_id' in spec.local_only


# ---------------------------------------------------------------------------
# 7. 內網過濾
# ---------------------------------------------------------------------------
def test_monitor_webhook_refuses_private_targets(app, monkeypatch, store_settings):
    from app.services import monitor_service, webhook_service
    calls = []
    monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: {'10.0.0.5'})
    monkeypatch.setattr(monitor_service.requests, 'post', lambda *a, **k: calls.append(a) or None)
    store_settings('monitor', webhook_url='https://internal.example.test/hook')
    with app.test_request_context('/'):
        from flask import g
        g.tenant_id = default_tenant_id(app)
        assert monitor_service.send_alert_now(g.tenant_id, 'info', 'x') == []
    assert calls == []
    monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: {'93.184.216.34'})
    with app.test_request_context('/'):
        from flask import g
        g.tenant_id = default_tenant_id(app)
        assert monitor_service.send_alert_now(g.tenant_id, 'info', 'x') == ['webhook']
    assert len(calls) == 1


def test_hub_rejects_private_smtp_ntp_and_printer_hosts(app, monkeypatch, config_flag):
    from app.errors import AppError
    from app.services import mail_service, monitor_service, webhook_service
    monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: {'172.18.0.3'})
    config_flag('SYNC_ROLE', 'hub')
    with app.test_request_context('/'):
        with pytest.raises(AppError):
            mail_service._connect({'host': 'postgres', 'port': 25})
        with pytest.raises(AppError):
            monitor_service.query_ntp('redis')
    admin = login_staff(app, 'audit2_hub_owner', 'owner')
    r = admin.post('/admin/printers', data={'action': 'add_printer', 'name': '內網', 'host': '172.18.0.3', 'port': '6379'},
                   follow_redirects=True)
    assert '內網' in r.get_data(as_text=True)
    with app.app_context():
        from app.models.printing import Printer
        assert Printer.query.filter_by(host='172.18.0.3').count() == 0
    # 門市主機 (standalone / node) 連區網印表機 / 寄信伺服器是正常的
    config_flag('SYNC_ROLE', 'standalone')
    with app.test_request_context('/'):
        webhook_service.check_public_host_on_hub('192.168.1.50')   # 不拋錯


# ---------------------------------------------------------------------------
# 8. 簡訊全店上限
# ---------------------------------------------------------------------------
def test_sms_tenant_quota(app, store_settings):
    store_settings('sms', provider='log', otp_on_register=True, tenant_hourly_limit=2, tenant_daily_limit=100)
    c = app.test_client()
    c.get('/?shop=default')
    with app.app_context():
        from app.extensions import db
        from app.models.member import OtpCode
        OtpCode.query.filter_by(tenant_id=default_tenant_id(app)).execution_options(skip_tenant_filter=True) \
            .delete(synchronize_session=False)
        db.session.commit()
    codes = []
    for i, phone in enumerate(('0977000001', '0977000002', '0977000003')):
        r = c.post('/api/otp/send', json={'phone': phone, 'purpose': 'register'}, headers={'X-Forwarded-For': f'10.77.0.{i}'})
        codes.append(r.status_code)
    assert codes == [200, 200, 429]


# ---------------------------------------------------------------------------
# 9. 人臉登入的工作階段：敏感操作需再輸入 PIN (第三輪 L1)
# ---------------------------------------------------------------------------
def _face_session(app, phone):
    """模擬人臉登入 (auth_factor=face) 的點餐機瀏覽器"""
    from app.models.user import User
    c = app.test_client()
    c.get('/?shop=default')
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            uid = User.query.filter_by(phone=phone).first().id
    with c.session_transaction() as sess:
        sess.update(user_id=uid, user_name='x', is_guest=False, auth_factor='face')
    return c


def test_face_login_session_requires_pin_for_sensitive_actions(app):
    phone = '0988123123'
    make_member(app, '人臉會員', phone, pin='6471', line_user_id='U-face')
    c = _face_session(app, phone)
    # 個資下載：GET 被拒 (需 PIN)，POST 錯誤 PIN 被拒並計次，正確後放行且之後 GET 也可以 (工作階段已升級)
    assert c.get('/privacy/my_data').status_code == 403
    assert c.post('/privacy/my_data', data={'pin': '0000'}).status_code == 403
    r = c.post('/privacy/my_data', data={'pin': '6471'})
    assert r.status_code == 200 and r.mimetype == 'application/json'
    assert c.get('/privacy/my_data').status_code == 200
    # 會員條碼：同樣規則 (JSON API 回 need_pin)
    c = _face_session(app, phone)
    r = c.get('/api/member_qr')
    assert r.status_code == 403 and r.get_json()['need_pin'] is True
    assert c.post('/api/member_qr', json={'action': 'confirm', 'pin': '6471'}).status_code == 200
    assert c.get('/api/member_qr').status_code == 200
    # 解除 LINE：需 PIN
    c = _face_session(app, phone)
    assert c.post('/auth/line/unbind').status_code == 403
    r = c.post('/auth/line/unbind', data={'pin': '6471'})
    assert r.status_code in (302, 303)
    # 以 PIN 登入的工作階段不受影響
    m = member_client(app, phone, '6471', ip='10.77.1.1')
    assert m.get('/privacy/my_data').status_code == 200 and m.get('/api/member_qr').status_code == 200
